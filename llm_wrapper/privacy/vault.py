"""Placeholder vault: maps placeholders to real values for one request.

Placeholders are ``<TYPE_xxxxxxxx>``: an HMAC of the value under the wrapper key and the
key scope (the conversation by default), so the same value keeps its placeholder across
turns of one conversation but is not linkable across conversations. On a collision the
suffix grows (12, then 16 hex characters).

Each entry records its origin. Restore only gives back values whose origin the policy's
``response_values`` allows: ``input_only`` restores only values the caller sent.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"<?\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*?)_([0-9a-fA-F]{8,16})\b>?")
CALLER_INPUT = "caller_input"


def load_key(path: str | None = None) -> bytes:
    """Wrapper key: WRAPPER_PRIVACY_KEY (hex) or a 0600 key file created on first use."""
    env = os.environ.get("WRAPPER_PRIVACY_KEY")
    if env:
        return bytes.fromhex(env)
    p = Path(path or "~/.llm_wrapper/key").expanduser()
    if p.exists():
        return bytes.fromhex(p.read_text().strip())
    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    return key


def value_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(slots=True)
class Entry:
    placeholder: str
    value: str
    type: str
    origin: str
    pattern_id: str


@dataclass
class Vault:
    key: bytes
    scope_key: str
    key_version: int = 1
    by_placeholder: dict[str, Entry] = field(default_factory=dict)
    by_value: dict[str, Entry] = field(default_factory=dict)

    def placeholder_for(self, value: str, type_: str, origin: str = CALLER_INPUT,
                        pattern_id: str = "") -> str:
        hit = self.by_value.get(value)
        if hit is not None:
            return hit.placeholder
        mac = hmac.new(self.key, f"{self.scope_key}\x00{type_}\x00{value}".encode(), hashlib.sha256).hexdigest()
        for n in (8, 12, 16):
            ph = f"<{type_}_{mac[:n]}>"
            other = self.by_placeholder.get(ph)
            if other is None or other.value == value:
                break
        else:  # pragma: no cover - 64-bit collision
            raise RuntimeError("placeholder collision could not be resolved")
        e = Entry(ph, value, type_, origin, pattern_id)
        self.by_placeholder[ph] = e
        self.by_value[value] = e
        return ph

    def lookup(self, type_: str, suffix: str) -> Entry | None:
        """Tolerant lookup: case-insensitive type and suffix, brackets optional."""
        ph = f"<{type_.upper()}_{suffix.lower()}>"
        return self.by_placeholder.get(ph)

    def __len__(self) -> int:
        return len(self.by_placeholder)

    def __bool__(self) -> bool:  # an empty vault is still a vault
        return True
