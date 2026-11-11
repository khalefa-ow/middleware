"""Request pipeline (v1: safe pass-through with routing).

Flow per request:
  identity -> route (policy rules) -> egress guard (redact + hint + leak check)
  -> size check (fits / reroute / fail clearly) -> call real model
  -> continue cut-off plain text -> restore -> usage + trace (background writer)

The whole request is one model call: there is no planning yet, so every request takes the
fast path. Caller params, tools and response_format are forwarded unchanged.
"""

from __future__ import annotations

import contextvars
import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import litellm

from . import size as size_mod
from .expr import evaluate
from .policy import LoadedPolicy
from .privacy import (Detector, EgressBlocked, RedactionReport, RestoreReport, StreamRestorer,
                      Vault, add_hint, leak_check, redact_payload, redact_response_format, restore_text,
                      value_hash)
from .storage import Store, dumps

_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("wrapper_depth", default=0)

# Params LiteLLM hands the provider that are not model parameters.
_IGNORED_PARAMS = {"metadata", "stream_options", "user", "litellm_session_id", "litellm_trace_id",
                   "mock_response", "extra_headers", "api_key", "api_base"}
_INTERNAL_MARK = "wrapper_internal"


class SelfCallError(RuntimeError):
    pass


@dataclass
class Prepared:
    request_id: str
    started: float
    session_key: str
    model: str
    route_id: str
    trusted: bool
    messages: list[dict]
    tools: list[dict] | None
    params: dict[str, Any]
    vault: Vault
    redaction: RedactionReport
    size: size_mod.SizeCheck
    caller_messages: list[dict]
    caller_metadata: dict[str, Any]
    source: str = "own_live"
    caller_response_format: Any = None
    """The caller's response_format as given; ``params`` holds the redacted copy that is sent."""
    fallback_from: str | None = None
    fallback_error: str | None = None
    spans: list[dict] = field(default_factory=list)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def session_key(messages: list[dict], metadata: dict[str, Any]) -> str:
    """Conversation identity: metadata.session_id, else a hash of the conversation prefix
    (first system + first user message), which stays stable as turns are appended."""
    sid = metadata.get("session_id") or metadata.get("conversation_id")
    if sid:
        return f"sid:{sid}"
    first_sys = next((_text_of(m.get("content")) for m in messages if m.get("role") == "system"), "")
    first_user = next((_text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
    return "prefix:" + hashlib.sha256(f"{first_sys}\x00{first_user}".encode()).hexdigest()[:24]


def route_context(messages: list[dict], params: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    system = "\n".join(_text_of(m.get("content")) for m in messages if m.get("role") == "system")
    users = [m for m in messages if m.get("role") == "user"]
    return {
        "metadata": metadata,
        "request": {
            "system": system,
            "last_user": _text_of(users[-1].get("content")) if users else "",
            "n_messages": len(messages),
            "chars": sum(len(_text_of(m.get("content"))) for m in messages),
            "has_tools": bool(params.get("tools")),
            "has_response_format": bool(params.get("response_format")),
            "stream": bool(params.get("stream")),
        },
    }


class Wrapper:
    def __init__(self, loaded: LoadedPolicy, store: Store | None, key: bytes):
        self.loaded = loaded
        self.policy = loaded.policy
        self.store = store
        self.key = key
        self.acompletion = litellm.acompletion  # seam for tests / alternative clients
        self.run_tag = os.environ.get("WRAPPER_RUN_TAG") or None  # set by `wrapper ab run`; stamped on every row
        pv = self.policy.privacy
        self.detector = Detector(pv.patterns, [(c.id, c.type, c.regex) for c in pv.custom_patterns],
                                 pv.allow_patterns, skip_code_blocks=not pv.redact_code_blocks)
        if store:
            store.put("policy_versions", {"hash": loaded.hash, "effective_yaml": loaded.effective_yaml,
                                          "source": loaded.source, "loaded_at": time.time()})

    # ------------------------------------------------------------ routing
    def choose_route(self, ctx: dict[str, Any]) -> tuple[str, str]:
        override = ctx["metadata"].get("wrapper_route")
        for r in self.policy.routes:
            if override == r.id or (override is None and evaluate(r.when, ctx)):
                return r.model, r.id
        return self.policy.models.default, "default"

    # ------------------------------------------------------------ prepare
    def _guard(self, messages, tools, model: str, vault: Vault, response_format: Any = None):
        trusted = self.policy.is_trusted(model)
        pv = self.policy.privacy
        if trusted or not pv.enabled:
            return messages, tools, response_format, RedactionReport(), trusted
        msgs, tls, report = redact_payload(messages, tools, vault, self.detector, pv.non_text, trusted)
        rf = redact_response_format(response_format, vault, self.detector, report)
        if len(vault) and pv.placeholder_hint:
            msgs = add_hint(msgs)
        if pv.leak_check:
            hits = leak_check({"messages": msgs, "tools": tls, "response_format": rf}, vault)
            if hits:
                raise EgressBlocked(f"leak check blocked the call to {model}: {len(hits)} value(s) still present")
        return msgs, tls, rf, report, trusted

    def prepare(self, messages: list[dict], params: dict[str, Any], metadata: dict[str, Any],
                force_model: str | None = None, source: str = "own_live") -> Prepared:
        """force_model is for in-process callers (experiments); it is not reachable from metadata."""
        depth = _DEPTH.get()
        if depth >= self.policy.limits.max_depth:
            raise SelfCallError(f"wrapper call depth {depth} exceeds limits.max_depth")
        started = time.time()
        rid = uuid.uuid4().hex
        skey = session_key(messages, metadata)
        scope = skey if self.policy.privacy.key_scope == "conversation" else "global"
        vault = Vault(self.key, scope)
        if force_model:
            model, route_id = force_model, "forced"
        else:
            model, route_id = self.choose_route(route_context(messages, params, metadata))
        tools = params.get("tools")
        fwd = {k: v for k, v in params.items() if k not in _IGNORED_PARAMS and k != "tools"}

        caller_rf = fwd.get("response_format")
        msgs, tls, rf, report, trusted = self._guard(messages, tools, model, vault, caller_rf)
        sc = size_mod.check(model, msgs, tls, self.policy, fwd.get("max_tokens"))
        if sc.model != model:  # rerouted: redo the guard if trust differs
            model = sc.model
            if self.policy.is_trusted(model) != trusted:
                msgs, tls, rf, report, trusted = self._guard(messages, tools, model, vault, caller_rf)
        if "response_format" in fwd:
            fwd = {**fwd, "response_format": rf}
        return Prepared(rid, started, skey, model, route_id, trusted, msgs, tls, fwd, vault, report, sc,
                        messages, metadata, source, caller_rf)

    # ------------------------------------------------------------ call
    async def _call(self, p: Prepared, messages: list[dict], **extra: Any) -> Any:
        kwargs = dict(p.params)
        kwargs.update(extra)
        if p.tools:
            kwargs["tools"] = p.tools
        kwargs["metadata"] = {_INTERNAL_MARK: True, "wrapper_request_id": p.request_id}
        kwargs.setdefault("timeout", self.policy.limits.call_timeout_s)
        token = _DEPTH.set(_DEPTH.get() + 1)
        try:
            return await self.acompletion(model=p.model, messages=messages, **kwargs)
        finally:
            _DEPTH.reset(token)

    def _span(self, p: Prepared, name: str, start: float, status: str = "ok", **attrs: Any) -> None:
        p.spans.append({"id": uuid.uuid4().hex, "request_id": p.request_id, "parent_id": None, "name": name,
                        "start": start, "end": time.time(), "status": status, "attrs_json": dumps(attrs)})

    async def acomplete(self, messages: list[dict], params: dict[str, Any], metadata: dict[str, Any],
                        force_model: str | None = None, source: str = "own_live") -> Any:
        p = None
        try:
            t0 = time.time()
            p = self.prepare(messages, params, metadata, force_model, source)
            self._span(p, "prepare", t0, route=p.route_id, model=p.model, placeholders=len(p.vault),
                       size=p.size.strategy, tokens=p.size.tokens)
            t1 = time.time()
            try:
                resp = await self._call(p, p.messages)
            except Exception as exc:
                target = self._fallback_target(exc, p, force_model)
                if target is None:
                    raise  # fallback off, not applicable, or not a configuration error: the caller sees the error
                t_fb = time.time()
                try:
                    p2 = self.prepare(messages, params, metadata, force_model=target, source=source)
                except Exception:
                    raise exc  # cannot prepare for the fallback (e.g. too large): the original error is the useful one
                p2.route_id, p2.fallback_from, p2.fallback_error = "fallback", p.model, type(exc).__name__
                self._span(p2, "prepare", t_fb, route=p2.route_id, model=p2.model, placeholders=len(p2.vault),
                           size=p2.size.strategy, tokens=p2.size.tokens)
                self._span(p2, "fallback", t_fb, from_model=p.model, to=p2.model, error=type(exc).__name__,
                           message=str(exc)[:300])
                p = p2
                t1 = time.time()
                resp = await self._call(p, p.messages)
            self._span(p, "model_call", t1, model=p.model)
            usage = _usage_of(resp)
            # continue cut-off plain text
            choice = resp.choices[0]
            text = choice.message.content or ""
            n = 0
            while (choice.finish_reason == "length" and self.policy.size.continue_output
                   and n < self.policy.size.max_continuations and size_mod.can_continue(params, choice.message)):
                n += 1
                t2 = time.time()
                more = await self._call(p, p.messages + [{"role": "assistant", "content": text},
                                                         {"role": "user", "content": size_mod.CONTINUE_PROMPT}])
                self._span(p, "continuation", t2, n=n)
                usage = _add_usage(usage, _usage_of(more))
                choice = more.choices[0]
                text += choice.message.content or ""
                resp.choices[0].finish_reason = choice.finish_reason
            if n:
                resp.choices[0].message.content = text
                resp.usage = _litellm_usage(usage)
            rr = self.restore_response(resp, p)
            self._record(p, resp, usage, rr, error=None, continuations=n)
            return resp
        except Exception as exc:
            if p is not None:
                self._record(p, None, {}, RestoreReport(), error=f"{type(exc).__name__}: {exc}")
            elif self.store:
                self._record_failed_prepare(messages, params, metadata, exc)
            raise

    def _fallback_target(self, exc: Exception, p: Prepared, force_model: str | None) -> str | None:
        """The model to retry on, or None. Only configuration errors on a routed (not forced) call qualify:
        never timeouts, rate limits, context-window or content-policy errors (those are not about the model
        choice), and never when the failing model already is the fallback target."""
        fb = self.policy.fallback
        if not fb.enabled or force_model is not None:
            return None
        if isinstance(exc, (litellm.ContextWindowExceededError, litellm.ContentPolicyViolationError)):
            return None
        kind = ("bad_request" if isinstance(exc, litellm.BadRequestError)
                else "not_found" if isinstance(exc, litellm.NotFoundError)
                else "auth" if isinstance(exc, litellm.AuthenticationError) else None)
        target = fb.to or self.policy.models.default
        if kind is None or kind not in fb.on or target == p.model:
            return None
        return target

    def restore_response(self, resp: Any, p: Prepared) -> RestoreReport:
        rr = RestoreReport()
        rv = self.policy.privacy.response_values
        for ch in resp.choices:
            msg = ch.message
            if isinstance(msg.content, str):
                msg.content = restore_text(msg.content, p.vault, rv, rr)
            for tc in msg.tool_calls or []:
                if tc.function and isinstance(tc.function.arguments, str):
                    tc.function.arguments = restore_text(tc.function.arguments, p.vault, rv, rr, json_escape=True)
        return rr

    # ------------------------------------------------------------ streaming
    async def astream(self, messages: list[dict], params: dict[str, Any], metadata: dict[str, Any]
                      ) -> AsyncIterator[dict]:
        """Yields GenericStreamingChunk dicts. Text is streamed through a placeholder-safe
        buffer; requests with tools are answered whole (tool arguments need full restore)."""
        if params.get("tools"):
            resp = await self.acomplete(messages, {k: v for k, v in params.items() if k != "stream"}, metadata)
            msg = resp.choices[0].message
            for i, tc in enumerate(msg.tool_calls or []):
                yield _chunk("", tool_use={"id": tc.id, "type": "function", "index": i,
                                           "function": {"name": tc.function.name,
                                                        "arguments": tc.function.arguments}})
            yield _chunk(msg.content or "", finished=True, finish_reason=resp.choices[0].finish_reason or "stop",
                         usage=_usage_block(_usage_of(resp)))
            return
        try:
            p = self.prepare(messages, params, metadata)
        except Exception as exc:
            if self.store:
                self._record_failed_prepare(messages, params, metadata, exc)
            raise
        restorer = StreamRestorer(p.vault, self.policy.privacy.response_values)
        t1 = time.time()
        usage: dict[str, int] = {}
        finish = "stop"
        parts: list[str] = []
        try:
            stream = await self._call(p, p.messages, stream=True, stream_options={"include_usage": True})
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = _usage_of(chunk)
                if not chunk.choices:
                    continue
                c = chunk.choices[0]
                if c.finish_reason:
                    finish = c.finish_reason
                delta = getattr(c.delta, "content", None) or ""
                if delta:
                    out = restorer.feed(delta)
                    if out:
                        parts.append(out)
                        yield _chunk(out)
            tail = restorer.flush()
            parts.append(tail)
            self._span(p, "model_call", t1, model=p.model, stream=True)
            yield _chunk(tail, finished=True, finish_reason=finish, usage=_usage_block(usage))
            fake = litellm.ModelResponse(model=p.model, choices=[{"message": {"role": "assistant",
                                                                             "content": "".join(parts)},
                                                                 "finish_reason": finish}])
            self._record(p, fake, usage, restorer.report, error=None, stream=True)
        except Exception as exc:
            self._record(p, None, usage, restorer.report, error=f"{type(exc).__name__}: {exc}", stream=True)
            raise

    # ------------------------------------------------------------ recording
    def _cost(self, model: str, usage: dict[str, int]) -> float | None:
        try:
            pin, pout = litellm.cost_per_token(model=model, prompt_tokens=usage.get("prompt_tokens", 0),
                                               completion_tokens=usage.get("completion_tokens", 0),
                                               cache_read_input_tokens=usage.get("cached_tokens", 0),
                                               cache_creation_input_tokens=usage.get("cache_write_tokens", 0))
            return pin + pout
        except Exception:
            return None

    def _record(self, p: Prepared, resp: Any, usage: dict[str, int], rr: RestoreReport, error: str | None,
                continuations: int = 0, stream: bool = False) -> None:
        cost = self._cost(p.model, usage) if usage else None
        info = {"request_id": p.request_id, "route": p.route_id, "model": p.model, "fast_path": True,
                "placeholders": len(p.vault), "restored": rr.restored, "masked": rr.masked,
                "unknown_placeholders": len(rr.unknown), "size_strategy": p.size.strategy,
                "continuations": continuations, "cost_usd": cost, "policy": self.loaded.hash}
        if p.fallback_from:
            info |= {"fallback_from": p.fallback_from, "fallback_error": p.fallback_error}
        if resp is not None:
            hp = getattr(resp, "_hidden_params", None)
            if isinstance(hp, dict):
                hp["wrapper"] = info
        if not self.store:
            return
        st = self.store
        rec = self.policy.storage.record_inputs
        st.put("requests", {
            "id": p.request_id, "ts": p.started, "source": p.source, "session_key": p.session_key,
            "user": p.caller_metadata.get("user"), "model_requested": "wrapper/auto", "model_used": p.model,
            "route_id": p.route_id, "policy_hash": self.loaded.hash, "fast_path": 1, "stream": int(stream),
            "input_json": dumps(p.caller_messages) if rec else None,
            "output_json": dumps(resp.choices[0].message.model_dump()) if (rec and resp is not None) else None,
            "params_json": dumps({k: v for k, v in p.params.items() if k != "response_format"}
                                 | self._recorded_response_format(p, rec)),
            "metadata_json": dumps(p.caller_metadata | {"wrapper": info}),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "cached_tokens": usage.get("cached_tokens"), "cache_write_tokens": usage.get("cache_write_tokens"),
            "cost_usd": cost, "latency_ms": (time.time() - p.started) * 1000,
            "finish_reason": resp.choices[0].finish_reason if resp is not None else None, "error": error,
            "content_hash": None, "run_tag": self.run_tag,
        })
        for s in p.spans:
            st.put("spans", s)
        st.put("egress_events", {"request_id": p.request_id, "span_id": None, "target": p.model,
                                 "trusted": int(p.trusted), "placeholders": len(p.vault),
                                 "non_text_parts": p.redaction.non_text_parts,
                                 "leak_blocked": int(bool(error and "leak check" in error)), "ts": time.time()})
        for pid, ph in p.redaction.matches:
            st.put("redactions", {"request_id": p.request_id, "pattern_id": pid, "placeholder": ph, "ts": time.time()})
        st.put("size_events", {"request_id": p.request_id, "model": p.model, "tokens": p.size.tokens,
                               "limit_tokens": p.size.available, "strategy": p.size.strategy,
                               "success": int(error is None), "detail": None, "ts": time.time()})
        for e in p.vault.by_placeholder.values():
            vh = value_hash(e.value)
            # with record_inputs off, keep only the hash (enough for delete-by-value), never the raw value
            st.put("vault", {"placeholder": e.placeholder, "scope_key": p.vault.scope_key,
                             "value": e.value if rec else None,
                             "value_hash": vh, "type": e.type, "origin": e.origin,
                             "key_version": p.vault.key_version, "created": time.time()})
            st.put("vault_refs", {"value_hash": vh, "request_id": p.request_id})

    @staticmethod
    def _recorded_response_format(p: Prepared, rec: bool) -> dict[str, Any]:
        """The caller's own schema when inputs are recorded (so replays are faithful), else just a marker."""
        if "response_format" not in p.params:
            return {}
        rf = p.caller_response_format
        return {"response_format": rf if (rec and isinstance(rf, dict)) else True}

    def _record_failed_prepare(self, messages, params, metadata, exc: Exception) -> None:
        # never store the raw input of a request we refused for privacy reasons
        self.store.put("requests", {
            "id": uuid.uuid4().hex, "ts": time.time(), "source": "own_live", "model_requested": "wrapper/auto",
            "policy_hash": self.loaded.hash, "fast_path": 1, "error": f"{type(exc).__name__}: {exc}",
            "metadata_json": dumps(metadata), "session_key": session_key(messages, metadata),
            "run_tag": self.run_tag,
        })


_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens")


def _usage_of(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if not u:
        return {}
    out = {k: int(getattr(u, k, 0) or 0) for k in _USAGE_KEYS[:3]}
    details = getattr(u, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if cached is None:  # DeepSeek's own field
        cached = getattr(u, "prompt_cache_hit_tokens", None)
    out["cached_tokens"] = int(cached or 0)
    # tokens written to the provider's cache (Anthropic bills them above the normal input price)
    written = getattr(u, "cache_creation_input_tokens", None)
    if not written and details is not None:
        written = getattr(details, "cache_creation_tokens", None) or getattr(details, "cache_write_tokens", None)
    out["cache_write_tokens"] = int(written or 0)
    return out


def _add_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: a.get(k, 0) + b.get(k, 0) for k in _USAGE_KEYS}


def _litellm_usage(u: dict[str, int]) -> Any:
    return litellm.Usage(prompt_tokens=u.get("prompt_tokens", 0), completion_tokens=u.get("completion_tokens", 0),
                         total_tokens=u.get("total_tokens", 0),
                         prompt_tokens_details={"cached_tokens": u.get("cached_tokens", 0)},
                         cache_creation_input_tokens=u.get("cache_write_tokens", 0))


def _usage_block(u: dict[str, int]) -> dict[str, int] | None:
    return {k: v for k, v in u.items() if k not in ("cached_tokens", "cache_write_tokens")} if u else None


def _chunk(text: str, finished: bool = False, finish_reason: str = "", usage=None, tool_use=None) -> dict:
    return {"text": text, "is_finished": finished, "finish_reason": finish_reason, "usage": usage,
            "index": 0, "tool_use": tool_use}
