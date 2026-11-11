"""Egress guard and restore.

``redact_payload`` runs on every outbound call to an untrusted target, right before
sending: it redacts message text (including tool-call arguments and tool results, which
the caller sent and so count as caller input), caller tool schemas (descriptions and enum
values), then runs a leak check that blocks the call if any known real value is still in
the payload. ``restore_*`` swaps placeholders back, tolerantly but only for ids in the
vault, and only for origins the policy allows.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .patterns import Detector, Match
from .vault import CALLER_INPUT, PLACEHOLDER_RE, Vault

STRICT_PLACEHOLDER = re.compile(r"<([A-Z][A-Z0-9_]*)_([0-9a-f]{8,16})>")
HINT = ("Some values are replaced by placeholders such as <EMAIL_1a2b3c4d>. "
        "Keep every placeholder exactly as written, including the angle brackets.")


class EgressBlocked(RuntimeError):
    """A real value would have left the machine; the call was not made."""


class NonTextBlocked(RuntimeError):
    pass


@dataclass
class RedactionReport:
    matches: list[tuple[str, str]] = field(default_factory=list)  # (pattern_id, placeholder)
    non_text_parts: int = 0


def redact_text(text: str, vault: Vault, detector: Detector, report: RedactionReport,
                origin: str = CALLER_INPUT) -> str:
    found: list[Match] = detector.find(text)
    if not found and not vault.by_value:
        return text
    out, pos = [], 0
    for m in found:
        ph = vault.placeholder_for(m.value, m.type, origin, m.pattern_id)
        report.matches.append((m.pattern_id, ph))
        out.append(text[pos:m.start])
        out.append(ph)
        pos = m.end
    out.append(text[pos:])
    result = "".join(out)
    # values already in the vault (from earlier text) are replaced wherever they recur,
    # including spots the detectors skip (e.g. inside code blocks)
    for value, e in vault.by_value.items():
        if len(value) >= 4 and value in result:
            result = result.replace(value, e.placeholder)
    return result


def _redact_json_strings(obj: Any, keys: tuple[str, ...], fn) -> Any:
    """Redact string values under the given keys (and all strings in enum lists)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                out[k] = fn(v)
            elif k == "enum" and isinstance(v, list):
                out[k] = [fn(i) if isinstance(i, str) else i for i in v]
            else:
                out[k] = _redact_json_strings(v, keys, fn)
        return out
    if isinstance(obj, list):
        return [_redact_json_strings(i, keys, fn) for i in obj]
    return obj


def redact_payload(messages: list[dict], tools: list[dict] | None, vault: Vault,
                   detector: Detector, non_text: str, trusted: bool,
                   ) -> tuple[list[dict], list[dict] | None, RedactionReport]:
    report = RedactionReport()
    fn = lambda s: redact_text(s, vault, detector, report)  # noqa: E731
    msgs = copy.deepcopy(messages)
    for msg in msgs:
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = fn(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    part["text"] = fn(part["text"])
                else:
                    report.non_text_parts += 1
                    if non_text == "block" or (non_text == "trusted_only" and not trusted):
                        raise NonTextBlocked(
                            f"non-text content ({part.get('type')}) may not be sent to an untrusted "
                            f"model (privacy.non_text={non_text})")
        for tc in msg.get("tool_calls") or []:
            f = tc.get("function") if isinstance(tc, dict) else None
            if f and isinstance(f.get("arguments"), str):
                f["arguments"] = fn(f["arguments"])
    new_tools = _redact_json_strings(copy.deepcopy(tools), ("description",), fn) if tools else tools
    return msgs, new_tools, report


def leak_check(payload: Any, vault: Vault) -> list[str]:
    """Return placeholders whose real value still appears in the outbound payload."""
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    hits = []
    for value, e in vault.by_value.items():
        if len(value) < 4:
            continue
        if value in blob or json.dumps(value, ensure_ascii=False)[1:-1] in blob:
            hits.append(e.placeholder)
    return hits


def add_hint(messages: list[dict]) -> list[dict]:
    """Append the keep-placeholders instruction to the first system message (or add one)."""
    msgs = list(messages)
    for i, m in enumerate(msgs):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            msgs[i] = {**m, "content": m["content"].rstrip() + "\n\n" + HINT}
            return msgs
    return [{"role": "system", "content": HINT}, *msgs]


# ---------------------------------------------------------------- restore


@dataclass
class RestoreReport:
    restored: int = 0
    masked: int = 0
    unknown: list[str] = field(default_factory=list)


def _allowed(origin: str, response_values: str) -> bool:
    return response_values == "all" or origin == CALLER_INPUT


def restore_text(text: str, vault: Vault, response_values: str, report: RestoreReport,
                 json_escape: bool = False) -> str:
    if not text:
        return text
    if len(vault):
        def sub(m: re.Match) -> str:
            e = vault.lookup(m.group(1), m.group(2))
            if e is None:
                return m.group(0)
            if not _allowed(e.origin, response_values):
                report.masked += 1
                return e.placeholder
            report.restored += 1
            return json.dumps(e.value)[1:-1] if json_escape else e.value
        text = PLACEHOLDER_RE.sub(sub, text)
    report.unknown += [m.group(0) for m in STRICT_PLACEHOLDER.finditer(text)
                       if vault.lookup(m.group(1), m.group(2)) is None]
    return text


class StreamRestorer:
    """Restores placeholders in streamed text, holding back a possible partial placeholder."""

    MAX_HOLD = 40

    def __init__(self, vault: Vault, response_values: str):
        self.vault, self.rv = vault, response_values
        self.buf = ""
        self.report = RestoreReport()

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        cut = self.buf.rfind("<")
        if cut == -1 or ">" in self.buf[cut:] or len(self.buf) - cut > self.MAX_HOLD:
            cut = len(self.buf)
            # also hold a trailing bare "TYPE_abc" run that may still be growing
            tail = re.search(r"[A-Za-z0-9_]{1,40}$", self.buf)
            if tail and "_" in tail.group(0):
                cut = tail.start()
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return restore_text(out, self.vault, self.rv, self.report)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return restore_text(out, self.vault, self.rv, self.report)
