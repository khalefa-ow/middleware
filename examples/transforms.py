"""Example context strategy for an experiment arm: ``transform: "python:transforms:keep_first_and_last"``."""

from __future__ import annotations


def keep_first_and_last(messages: list[dict]) -> list[dict]:
    """Keep system messages, the first user message and the last four messages; drop the middle turns."""
    system = [m for m in messages if m.get("role") == "system"]
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    tail = messages[-4:]
    out = list(system)
    for m in ([first_user] if first_user else []) + tail:
        if not any(m is x for x in out):
            out.append(m)
    return out
