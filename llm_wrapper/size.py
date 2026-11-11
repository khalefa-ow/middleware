"""Message size checks.

Every outbound call is measured on its final payload (after redaction). Available input =
context window - reserved output - margin. A request that does not fit is rerouted to a
larger-context model when the policy allows (non-lossy), otherwise it fails clearly with
the numbers. v1 has no lossy strategies (trim/summarize).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import litellm

from .policy import Policy


class ContextTooLarge(ValueError):
    def __init__(self, model: str, tokens: int, available: int):
        self.model, self.tokens, self.available = model, tokens, available
        super().__init__(
            f"request needs ~{tokens} input tokens but {model} allows {available} "
            f"(over by {tokens - available}); no non-lossy strategy fits it")


@dataclass
class SizeCheck:
    model: str
    tokens: int
    available: int | None
    strategy: str  # fits | reroute | unknown_limit

    @property
    def fits(self) -> bool:
        return self.available is None or self.tokens <= self.available


@lru_cache(maxsize=256)
def _litellm_limits(model: str) -> tuple[int | None, int | None]:
    try:
        info = litellm.get_model_info(model)
        return info.get("max_input_tokens") or info.get("max_tokens"), info.get("max_output_tokens")
    except Exception:
        return None, None


def model_limits(model: str, policy: Policy) -> tuple[int | None, int | None]:
    """(max input tokens, max output tokens); policy overrides LiteLLM's model info."""
    max_in, max_out = _litellm_limits(model)
    return (policy.size.limits.get(model) or max_in), max_out


def upper_bound_tokens(messages: list[dict], tools: list[dict] | None) -> int:
    """No tokenizer produces more tokens than UTF-8 bytes (plus a little per-message framing),
    so this is a safe ceiling that costs one serialization instead of a tokenizer pass."""
    return len(json.dumps([messages, tools], ensure_ascii=False, default=str).encode()) + 8 * len(messages)


def count_tokens(model: str, messages: list[dict], tools: list[dict] | None) -> int:
    try:
        return litellm.token_counter(model=model, messages=messages, tools=tools)
    except Exception:
        import json
        return len(json.dumps(messages, default=str)) // 3 + (len(json.dumps(tools)) // 3 if tools else 0)


def available_input(model: str, policy: Policy, max_tokens: int | None) -> int | None:
    max_in, max_out = model_limits(model, policy)
    if not max_in:
        return None
    reserve = max_tokens or min(policy.size.reserve_output_tokens, max_out or policy.size.reserve_output_tokens)
    # when max_input_tokens is the full window, the reserve comes out of it
    return int((max_in - reserve) * (1 - policy.size.margin)) if max_in > reserve else int(max_in * (1 - policy.size.margin))


def check(model: str, messages: list[dict], tools: list[dict] | None, policy: Policy,
          max_tokens: int | None) -> SizeCheck:
    avail = available_input(model, policy, max_tokens)
    ceiling = upper_bound_tokens(messages, tools)
    if avail is not None and ceiling <= avail:
        # surely fits: skip the exact count (the fast path's main cost)
        return SizeCheck(model, ceiling // 4, avail, "fits")
    tokens = count_tokens(model, messages, tools)
    if avail is None:
        return SizeCheck(model, tokens, None, "unknown_limit")
    if tokens <= avail:
        return SizeCheck(model, tokens, avail, "fits")
    if policy.size.reroute:
        for alt in policy.models.large_context:
            alt_avail = available_input(alt, policy, max_tokens)
            alt_tokens = count_tokens(alt, messages, tools)
            if alt_avail is None or alt_tokens <= alt_avail:
                return SizeCheck(alt, alt_tokens, alt_avail, "reroute")
    raise ContextTooLarge(model, tokens, avail)


CONTINUE_PROMPT = ("Your previous answer was cut off by the length limit. Continue exactly where it "
                   "stopped, without repeating anything and without any preamble.")


def can_continue(params: dict[str, Any], message: Any) -> bool:
    """Only plain text the caller did not cap or constrain is continued."""
    if params.get("max_tokens") or params.get("max_completion_tokens"):
        return False  # the cut-off is what the caller asked for
    if params.get("response_format") or params.get("tools"):
        return False
    return not getattr(message, "tool_calls", None)
