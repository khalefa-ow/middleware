"""Import OpenAI-style chat logs (JSONL, one conversation per line).

Each record holds a message list (``--messages-field``, e.g. ``conversation`` for WildChat,
``trajectory`` for OpenHands datasets). Every assistant turn becomes one call row: its
prompt is everything before it, its completion is the turn itself. Token counts come from
LiteLLM's tokenizer for ``tokenizer_model``. Only the messages new since the previous call
are stored (``input_json``), with ``cached_prefix_tokens`` in metadata: the exact number of
prompt tokens that repeat the previous call's prompt + answer, which is what provider
prompt caching can discount. Public records are redacted on import.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import litellm

from ..privacy import Detector, RedactionReport, Vault
from ..privacy.egress import redact_text
from ..storage import Store, dumps

KEEP = ("role", "content", "tool_calls", "name", "tool_call_id")
PER_MESSAGE_OVERHEAD = 4


@dataclass
class ChatImportStats:
    records: int = 0
    calls: int = 0
    bad_lines: list[str] = field(default_factory=list)


def _get(rec: dict, path: str | None) -> Any:
    if not path:
        return None
    cur: Any = rec
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _norm(m: dict) -> dict:
    out = {k: m.get(k) for k in KEEP if m.get(k) is not None}
    if not isinstance(out.get("content"), (str, list)) and "content" in out:
        out["content"] = str(out["content"])
    return out


def _tokens(m: dict, model: str) -> int:
    text = m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content") or "")
    if m.get("tool_calls"):
        text += json.dumps(m["tool_calls"])
    try:
        return litellm.token_counter(model=model, text=text) + PER_MESSAGE_OVERHEAD
    except Exception:
        return len(text) // 4 + PER_MESSAGE_OVERHEAD


def import_chat_jsonl(path: str, store: Store, messages_field: str, source: str = "public",
                      tokenizer_model: str = "gpt-4o", id_field: str | None = None,
                      model_field: str | None = None, outcome_field: str | None = None,
                      caller_kind: str = "chat", redact: bool = True, limit: int | None = None,
                      key: bytes = b"public-import") -> ChatImportStats:
    stats = ChatImportStats()
    detector = Detector(["email", "phone", "card", "ssn", "api_key"])
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if limit and stats.records >= limit:
                break
            try:
                rec = json.loads(line)
                raw = rec[messages_field]
                assert isinstance(raw, list)
            except Exception:
                stats.bad_lines.append(f"{path}:{n}")
                continue
            stats.records += 1
            conv_id = str(_get(rec, id_field) or hashlib.sha256(line.encode()).hexdigest()[:24])
            model = str(_get(rec, model_field) or "unknown")
            outcome = _get(rec, outcome_field)
            msgs = [_norm(m) for m in raw if isinstance(m, dict)]
            if redact:  # public data is redacted before it is stored (conversation-scoped placeholders)
                vault = Vault(key, conv_id)
                rep = RedactionReport()
                for m in msgs:
                    if isinstance(m.get("content"), str):
                        m["content"] = redact_text(m["content"], vault, detector, rep)
                redactions = [pid for pid, _ in rep.matches]
            else:
                redactions = []
            toks = [_tokens(m, tokenizer_model) for m in msgs]
            prev_end = -1  # index of the previous assistant turn
            turn = 0
            for k, m in enumerate(msgs):
                if m.get("role") != "assistant" or k == 0:
                    continue
                prompt_tokens = sum(toks[:k])
                cached = sum(toks[:prev_end + 1]) if prev_end >= 0 else 0
                delta = msgs[prev_end + 1:k]
                meta = {
                    "role": caller_kind, "run_id": conv_id, "turn": turn,
                    "run_success": None if outcome is None else bool(outcome),
                    "cached_prefix_tokens": cached, "delta_input": True, "n_messages": k,
                    "public_model": model, "redactions": redactions if turn == 0 else [],
                    "has_tools": bool(m.get("tool_calls")),
                }
                store.put("requests", {
                    "id": uuid.uuid4().hex, "ts": None, "source": source, "session_key": f"{path}:{conv_id}",
                    "user": None, "model_requested": model, "model_used": model, "route_id": caller_kind,
                    "policy_hash": None, "fast_path": None, "stream": 0,
                    "input_json": dumps(delta), "output_json": dumps(m), "params_json": None,
                    "metadata_json": dumps(meta), "prompt_tokens": prompt_tokens,
                    "completion_tokens": toks[k], "cost_usd": None, "latency_ms": None, "finish_reason": None,
                    "error": None,
                    "content_hash": hashlib.sha256(f"{source}\x00{conv_id}\x00{k}".encode()).hexdigest(),
                })
                stats.calls += 1
                prev_end = k
                turn += 1
    store.flush()
    return stats
