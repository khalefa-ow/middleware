import json

import litellm
import pytest

import llm_wrapper
from llm_wrapper.pipeline import SelfCallError, _DEPTH
from llm_wrapper.privacy import EgressBlocked, NonTextBlocked
from llm_wrapper.size import ContextTooLarge

from .conftest import FakeModel

MSGS = [{"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Email jane@acme.com about card 4111 1111 1111 1111."}]


async def test_redacts_outbound_and_restores_reply(make_wrapper, store):
    fake = FakeModel()
    w = make_wrapper(fake)
    # reply echoes whatever placeholder the model saw
    async def echo(**kw):
        user = kw["messages"][-1]["content"]
        ph = user.split()[1]
        fake.replies = [f"Sent to {ph}."]
        return await FakeModel.__call__(fake, **kw)
    w.acompletion = echo
    resp = await w.acomplete(MSGS, {"temperature": 0.1}, {"session_id": "s1"})
    assert "jane@acme.com" not in fake.sent and "4111" not in fake.sent
    assert "Keep every placeholder" in fake.calls[0]["messages"][0]["content"]
    assert fake.calls[0]["temperature"] == 0.1
    assert fake.calls[0]["metadata"]["wrapper_internal"] is True
    assert resp.choices[0].message.content == "Sent to jane@acme.com."
    store.flush()
    row = store.query("SELECT * FROM requests")[0]
    assert row["model_used"] == "openai/gpt-4o-mini" and row["prompt_tokens"] == 10 and row["error"] is None
    assert {r["pattern_id"] for r in store.query("SELECT pattern_id FROM redactions")} == {"email", "card"}


async def test_no_hint_without_placeholders(make_wrapper):
    fake = FakeModel()
    w = make_wrapper(fake)
    await w.acomplete([{"role": "user", "content": "hi"}], {}, {})
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


async def test_trusted_model_gets_real_values(make_wrapper):
    fake = FakeModel()
    w = make_wrapper(fake, privacy={"trusted": ["openai/gpt-4o-mini"]})
    await w.acomplete(MSGS, {}, {})
    assert "jane@acme.com" in fake.sent


async def test_tools_and_tool_args(make_wrapper):
    fake = FakeModel(tool_args=None)
    w = make_wrapper(fake)
    tools = [{"type": "function", "function": {"name": "send", "description": "send to jane@acme.com",
                                               "parameters": {"type": "object", "properties": {
                                                   "to": {"type": "string", "enum": ["jane@acme.com", "x"]}}}}}]
    async def reply(**kw):
        ph = kw["tools"][0]["function"]["parameters"]["properties"]["to"]["enum"][0]
        fake.tool_args = json.dumps({"to": ph})
        return await FakeModel.__call__(fake, **kw)
    w.acompletion = reply
    resp = await w.acomplete(MSGS, {"tools": tools}, {})
    assert "jane@acme.com" not in fake.sent
    assert json.loads(resp.choices[0].message.tool_calls[0].function.arguments) == {"to": "jane@acme.com"}


async def test_history_tool_results_count_as_caller_input(make_wrapper):
    fake = FakeModel()
    w = make_wrapper(fake)
    msgs = [{"role": "user", "content": "look up bob"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "t", "type": "function", "function": {"name": "crm", "arguments": '{"q": "bob"}'}}]},
            {"role": "tool", "tool_call_id": "t", "content": "bob@corp.io"}]
    async def reply(**kw):
        fake.replies = [f"It is {kw['messages'][-1]['content']}"]
        return await FakeModel.__call__(fake, **kw)
    w.acompletion = reply
    resp = await w.acomplete(msgs, {}, {})
    assert "bob@corp.io" not in fake.sent
    assert resp.choices[0].message.content == "It is bob@corp.io"  # restored: the caller sent it


async def test_leak_check_blocks(make_wrapper, monkeypatch):
    fake = FakeModel()
    w = make_wrapper(fake)
    import llm_wrapper.pipeline as pl
    monkeypatch.setattr(pl, "add_hint", lambda m: m + [{"role": "user", "content": "jane@acme.com"}])
    with pytest.raises(EgressBlocked):
        await w.acomplete(MSGS, {}, {})
    assert fake.calls == []


async def test_non_text_blocked_for_untrusted(make_wrapper):
    w = make_wrapper(FakeModel())
    msgs = [{"role": "user", "content": [{"type": "text", "text": "what is this"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    with pytest.raises(NonTextBlocked):
        await w.acomplete(msgs, {}, {})
    w2 = make_wrapper(FakeModel(), privacy={"non_text": "pass"})
    await w2.acomplete(msgs, {}, {})


async def test_routes(make_wrapper):
    fake = FakeModel()
    w = make_wrapper(fake, routes=[
        {"id": "plan", "when": "metadata.role == 'planner'", "model": "openai/gpt-4o"},
        {"id": "big", "when": "request.chars > 10000", "model": "openai/gpt-4.1"}])
    await w.acomplete([{"role": "user", "content": "x"}], {}, {"role": "planner"})
    await w.acomplete([{"role": "user", "content": "x"}], {}, {})
    await w.acomplete([{"role": "user", "content": "x"}], {}, {"wrapper_route": "big"})
    assert [c["model"] for c in fake.calls] == ["openai/gpt-4o", "openai/gpt-4o-mini", "openai/gpt-4.1"]


async def test_size_fail_clearly_and_reroute(make_wrapper):
    big = [{"role": "user", "content": "word " * 3000}]
    w = make_wrapper(FakeModel(), size={"limits": {"openai/gpt-4o-mini": 1000}, "reserve_output_tokens": 100})
    with pytest.raises(ContextTooLarge, match="over by"):
        await w.acomplete(big, {}, {})
    fake = FakeModel()
    w = make_wrapper(fake, models={"default": "openai/gpt-4o-mini", "large_context": ["openai/gpt-4.1"]},
                     size={"limits": {"openai/gpt-4o-mini": 1000}, "reserve_output_tokens": 100})
    await w.acomplete(big, {}, {})
    assert fake.calls[0]["model"] == "openai/gpt-4.1"


async def test_continuation_only_when_uncapped(make_wrapper):
    fake = FakeModel(replies=["part one ", "part two"], finish=["length", "stop"])
    w = make_wrapper(fake)
    resp = await w.acomplete([{"role": "user", "content": "long"}], {}, {})
    assert resp.choices[0].message.content == "part one part two" and len(fake.calls) == 2
    assert resp.usage.prompt_tokens == 20
    fake = FakeModel(replies=["part one "], finish=["length"])
    w = make_wrapper(fake)
    resp = await w.acomplete([{"role": "user", "content": "long"}], {"max_tokens": 5}, {})
    assert len(fake.calls) == 1 and resp.choices[0].finish_reason == "length"


async def test_self_call_guard(make_wrapper):
    w = make_wrapper(FakeModel())
    tok = _DEPTH.set(1)
    try:
        with pytest.raises(SelfCallError):
            await w.acomplete([{"role": "user", "content": "x"}], {}, {})
    finally:
        _DEPTH.reset(tok)


async def test_stream_restores_across_chunks(make_wrapper):
    fake = FakeModel()
    w = make_wrapper(fake)
    async def reply(**kw):
        ph = kw["messages"][-1]["content"].split()[1]
        fake.replies = [f"Writing to {ph} now"]
        return await FakeModel.__call__(fake, **kw)
    w.acompletion = reply
    chunks = [c async for c in w.astream(MSGS, {"stream": True}, {})]
    assert "".join(c["text"] for c in chunks) == "Writing to jane@acme.com now"
    assert chunks[-1]["is_finished"] and chunks[-1]["usage"]["total_tokens"] == 10


def test_end_to_end_through_litellm(tmp_path):
    pol = tmp_path / "p.yaml"
    pol.write_text("models: {default: openai/gpt-4o-mini}\n")
    handler = llm_wrapper.register(str(pol), str(tmp_path / "e2e.db"))
    fake = FakeModel(replies=["hello"])
    handler.wrapper.acompletion = fake
    from pydantic import BaseModel

    class Out(BaseModel):
        a: int

    r = litellm.completion(model="wrapper/auto", messages=MSGS, temperature=0.3, response_format=Out,
                           metadata={"role": "planner", "session_id": "abc"})
    assert r.choices[0].message.content == "hello"
    sent = fake.calls[0]
    assert sent["model"] == "openai/gpt-4o-mini" and sent["temperature"] == 0.3
    assert sent["response_format"]["type"] == "json_schema"   # forwarded untouched
    assert "jane@acme.com" not in fake.sent

    with pytest.raises(Exception, match="SelfCallError|depth"):
        bad = tmp_path / "bad.yaml"
        bad.write_text("models: {default: openai/gpt-4o-mini}\n")
        h2 = llm_wrapper.register(str(bad), str(tmp_path / "e2e2.db"))
        async def recurse(**kw):
            return await litellm.acompletion(model="wrapper/auto", messages=kw["messages"])
        h2.wrapper.acompletion = recurse
        litellm.completion(model="wrapper/auto", messages=[{"role": "user", "content": "x"}])


async def test_async_end_to_end(tmp_path):
    pol = tmp_path / "p.yaml"
    pol.write_text("models: {default: openai/gpt-4o-mini}\n")
    handler = llm_wrapper.register(str(pol), str(tmp_path / "a.db"))
    handler.wrapper.acompletion = FakeModel(replies=["hi there"])
    r = await litellm.acompletion(model="wrapper/auto", messages=[{"role": "user", "content": "x"}])
    assert r.choices[0].message.content == "hi there"
    s = await litellm.acompletion(model="wrapper/auto", messages=[{"role": "user", "content": "x"}], stream=True)
    text = "".join([c.choices[0].delta.content or "" async for c in s])
    assert text == "hi there"


async def test_prefix_byte_stable_across_turns_and_cached_tokens_recorded(make_wrapper, store):
    fake = FakeModel()
    w = make_wrapper(fake)
    async def cached_reply(**kw):
        r = await FakeModel.__call__(fake, **kw)
        r.usage = litellm.Usage(prompt_tokens=100, completion_tokens=5, total_tokens=105,
                                prompt_tokens_details={"cached_tokens": 80})
        return r
    w.acompletion = cached_reply
    conv = [{"role": "system", "content": "Lead is jane@acme.com, phone +1 415 555 0100."},
            {"role": "user", "content": "hi"}]
    for i in range(3):
        await w.acomplete(conv, {}, {})
        conv = conv + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": f"more {i}"}]
    first = json.dumps(fake.calls[0]["messages"])
    assert all(json.dumps(c["messages"]).startswith(first[:-1]) for c in fake.calls)  # earlier payload is a prefix
    assert "jane@acme.com" not in fake.sent
    store.flush()
    rows = store.query("SELECT cached_tokens, cost_usd FROM requests")
    assert [r["cached_tokens"] for r in rows] == [80, 80, 80]


async def test_record_inputs_false_keeps_raw_values_out_of_every_table(make_wrapper, store):
    from llm_wrapper.privacy import value_hash
    fake = FakeModel()
    w = make_wrapper(fake, storage={"record_inputs": False})

    async def echo(**kw):
        ph = kw["messages"][-1]["content"].split()[1]
        fake.replies = [f"Sent to {ph}."]
        return await FakeModel.__call__(fake, **kw)
    w.acompletion = echo
    resp = await w.acomplete(MSGS, {}, {})
    assert "jane@acme.com" in resp.choices[0].message.content  # caller still gets the restored value
    store.flush()
    for table in ("requests", "vault", "redactions", "spans", "egress_events", "size_events", "vault_refs"):
        for row in store.query(f"SELECT * FROM {table}"):
            assert "jane@acme.com" not in json.dumps([str(v) for v in tuple(row)]), table
    assert store.query("SELECT count(*) AS n FROM vault")[0]["n"] >= 1
    assert len(store.requests_with_value(value_hash("jane@acme.com"))) == 1


async def test_stream_refused_before_model_call_is_recorded(make_wrapper, store):
    w = make_wrapper(FakeModel(), size={"limits": {"openai/gpt-4o-mini": 1000}, "reserve_output_tokens": 100})
    big = [{"role": "user", "content": "word " * 3000}]
    with pytest.raises(ContextTooLarge):
        _ = [c async for c in w.astream(big, {"stream": True}, {})]
    store.flush()
    rows = store.query("SELECT * FROM requests")
    assert len(rows) == 1 and rows[0]["error"].startswith("ContextTooLarge") and rows[0]["input_json"] is None


async def test_response_format_dict_is_recorded_as_given(make_wrapper, store):
    schema = {"type": "json_schema", "json_schema": {"name": "p", "schema": {"type": "object"}}}
    w = make_wrapper(FakeModel())
    await w.acomplete([{"role": "user", "content": "x"}], {"response_format": schema}, {})
    store.flush()
    assert json.loads(store.query("SELECT params_json FROM requests")[0]["params_json"])["response_format"] == schema
    w2 = make_wrapper(FakeModel(), storage={"record_inputs": False})
    await w2.acomplete([{"role": "user", "content": "y"}], {"response_format": schema}, {})
    store.flush()
    marks = [json.loads(r["params_json"])["response_format"] for r in store.query("SELECT params_json FROM requests")]
    assert True in marks


async def test_response_format_schema_is_redacted_for_untrusted_model_only(make_wrapper, store):
    schema = {"type": "json_schema", "json_schema": {"name": "p", "schema": {
        "type": "object", "description": "mail jane@acme.com",
        "properties": {"to": {"type": "string", "enum": ["jane@acme.com", "bob@other.org"]}}}}}
    fake = FakeModel()
    w = make_wrapper(fake)
    await w.acomplete([{"role": "user", "content": "hi"}], {"response_format": schema}, {})
    sent = json.dumps(fake.calls[0]["response_format"])
    assert "jane@acme.com" not in sent and "bob@other.org" not in sent and "<EMAIL_" in sent
    store.flush()
    rec = json.loads(store.query("SELECT params_json FROM requests")[0]["params_json"])["response_format"]
    assert rec == schema  # the caller's original is what gets recorded
    fake2 = FakeModel()
    w2 = make_wrapper(fake2, privacy={"trusted": ["openai/gpt-4o-mini"]})
    await w2.acomplete([{"role": "user", "content": "hi"}], {"response_format": schema}, {})
    assert fake2.calls[0]["response_format"] == schema


async def test_response_format_leak_check_and_reroute_guard(make_wrapper):
    # a value that appears only in the schema is found by the guard on the rerouted model too
    schema = {"type": "json_schema", "json_schema": {"name": "p", "schema": {
        "type": "object", "properties": {"to": {"enum": ["jane@acme.com"]}}}}}
    fake = FakeModel()
    w = make_wrapper(fake, models={"default": "openai/gpt-4o-mini", "large_context": ["openai/gpt-4.1"]},
                     size={"limits": {"openai/gpt-4o-mini": 1000}, "reserve_output_tokens": 100},
                     privacy={"trusted": ["openai/gpt-4o-mini"]})
    big = [{"role": "user", "content": "word " * 3000}]
    await w.acomplete(big, {"response_format": schema}, {})
    assert fake.calls[0]["model"] == "openai/gpt-4.1"   # rerouted to an untrusted model
    assert "jane@acme.com" not in json.dumps(fake.calls[0]["response_format"])


async def test_cache_control_markers_survive_redaction(make_wrapper):
    fake = FakeModel()
    w = make_wrapper(fake)
    msgs = [{"role": "system", "content": [
                {"type": "text", "text": "Long context about jane@acme.com", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "second part"}]},
            {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}]
    await w.acomplete(msgs, {}, {})
    sent = fake.calls[0]["messages"]
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}      # same part, same value
    assert "cache_control" not in sent[0]["content"][1]
    assert sent[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "jane@acme.com" not in fake.sent and "<EMAIL_" in sent[0]["content"][0]["text"]


async def test_hint_with_list_system_content_is_appended_not_a_new_message(make_wrapper):
    fake = FakeModel()
    w = make_wrapper(fake)
    msgs = [{"role": "system", "content": [{"type": "text", "text": "Mail jane@acme.com",
                                            "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": "hi"}]
    await w.acomplete(msgs, {}, {})
    sent = fake.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]            # no extra system message in front
    parts = sent[0]["content"]
    assert len(parts) == 2 and parts[1]["text"].strip().startswith("Some values are replaced")
    assert parts[0]["cache_control"] == {"type": "ephemeral"}          # the cached part is untouched


async def test_cache_write_tokens_recorded_and_priced(make_wrapper, store):
    import litellm as ll
    model = "claude-haiku-4-5-20251001"

    def reply_with(creation):
        async def f(**kw):
            r = await FakeModel.__call__(FakeModel(), **kw)
            r.usage = ll.Usage(prompt_tokens=100, completion_tokens=10, total_tokens=110,
                               cache_creation_input_tokens=creation, cache_read_input_tokens=20)
            return r
        return f

    w = make_wrapper(FakeModel(), models={"default": model})
    w.acompletion = reply_with(40)
    resp = await w.acomplete([{"role": "user", "content": "x"}], {}, {})
    w.acompletion = reply_with(0)
    await w.acomplete([{"role": "user", "content": "y"}], {}, {})
    store.flush()
    rows = store.query("SELECT cached_tokens, cache_write_tokens, cost_usd FROM requests ORDER BY ts")
    with_write, without = rows[0], rows[1]
    assert (with_write["cached_tokens"], with_write["cache_write_tokens"]) == (20, 40)
    assert without["cache_write_tokens"] == 0
    assert with_write["cost_usd"] > without["cost_usd"]          # cache writes cost more than plain input
    exp_in, exp_out = ll.cost_per_token(model=model, prompt_tokens=100, completion_tokens=10,
                                        cache_read_input_tokens=20, cache_creation_input_tokens=40)
    assert with_write["cost_usd"] == pytest.approx(exp_in + exp_out)
    assert resp.choices[0].message.content


def test_older_databases_gain_the_cache_write_column(tmp_path):
    import sqlite3
    from llm_wrapper.storage import Store
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, ts REAL, source TEXT, session_key TEXT, user TEXT, "
              "model_requested TEXT, model_used TEXT, route_id TEXT, policy_hash TEXT, fast_path INTEGER, "
              "stream INTEGER, input_json TEXT, output_json TEXT, params_json TEXT, metadata_json TEXT, "
              "prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, latency_ms REAL, "
              "finish_reason TEXT, error TEXT, content_hash TEXT)")
    c.commit()
    c.close()
    s = Store(p)
    cols = {r["name"] for r in s.query("PRAGMA table_info(requests)")}
    s.close()
    assert {"cached_tokens", "cache_write_tokens", "run_tag"} <= cols


class FailsFor(FakeModel):
    """Raises the given LiteLLM error for the listed models; answers normally for any other."""

    def __init__(self, models, error, **kw):
        super().__init__(**kw)
        self.models, self.error = set(models), error

    async def __call__(self, **kw):
        if kw["model"] in self.models:
            self.calls.append(kw)
            raise self.error(message="boom", model=kw["model"], llm_provider="openai")
        return await super().__call__(**kw)


ROUTE_TO_WEIRD = [{"id": "r", "when": "true", "model": "openai/weird"}]


def _fb(**over):
    return {"fallback": {"enabled": True} | over, "routes": ROUTE_TO_WEIRD}


async def test_fallback_retries_once_on_the_default_model_and_records_one_row(make_wrapper, store):
    for err in (litellm.BadRequestError, litellm.NotFoundError, litellm.AuthenticationError):
        fake = FailsFor(["openai/weird"], err)
        w = make_wrapper(fake, **_fb())
        resp = await w.acomplete([{"role": "user", "content": "hi"}], {}, {})
        assert [c["model"] for c in fake.calls] == ["openai/weird", "openai/gpt-4o-mini"]
        assert resp.choices[0].message.content == "ok"
        assert resp._hidden_params["wrapper"]["fallback_from"] == "openai/weird"
        assert resp._hidden_params["wrapper"]["fallback_error"] == err.__name__
    store.flush()
    rows = store.query("SELECT model_used, route_id, error, metadata_json, id FROM requests")
    assert len(rows) == 3 and all(r["model_used"] == "openai/gpt-4o-mini" and r["error"] is None for r in rows)
    assert all(r["route_id"] == "fallback" for r in rows)
    assert json.loads(rows[0]["metadata_json"])["wrapper"]["fallback_from"] == "openai/weird"
    spans = {s["name"] for s in store.query("SELECT name FROM spans WHERE request_id=?", (rows[0]["id"],))}
    assert "fallback" in spans


async def test_fallback_off_by_default_and_error_reaches_the_caller_unchanged(make_wrapper, store):
    fake = FailsFor(["openai/weird"], litellm.BadRequestError)
    w = make_wrapper(fake, routes=ROUTE_TO_WEIRD)             # fallback not configured at all
    with pytest.raises(litellm.BadRequestError):
        await w.acomplete([{"role": "user", "content": "hi"}], {}, {})
    assert len(fake.calls) == 1
    w = make_wrapper(fake, routes=ROUTE_TO_WEIRD, fallback={"enabled": False})
    with pytest.raises(litellm.BadRequestError):
        await w.acomplete([{"role": "user", "content": "hi"}], {}, {})
    store.flush()
    assert all(r["error"].startswith("BadRequestError") for r in store.query("SELECT error FROM requests"))


async def test_fallback_not_used_when_the_failing_model_is_the_target_or_forced_or_kind_not_listed(make_wrapper):
    fake = FailsFor(["openai/gpt-4o-mini"], litellm.BadRequestError)       # the default itself fails
    w = make_wrapper(fake, fallback={"enabled": True})
    with pytest.raises(litellm.BadRequestError):
        await w.acomplete([{"role": "user", "content": "hi"}], {}, {})
    assert len(fake.calls) == 1
    fake = FailsFor(["openai/weird"], litellm.BadRequestError)
    w = make_wrapper(fake, **_fb())
    with pytest.raises(litellm.BadRequestError):                           # forced calls (experiments) never fall back
        await w.acomplete([{"role": "user", "content": "hi"}], {}, {}, force_model="openai/weird")
    assert len(fake.calls) == 1
    fake = FailsFor(["openai/weird"], litellm.NotFoundError)
    w = make_wrapper(fake, **_fb(on=["bad_request"]))
    with pytest.raises(litellm.NotFoundError):
        await w.acomplete([{"role": "user", "content": "hi"}], {}, {})
    assert len(fake.calls) == 1


async def test_fallback_never_for_context_window_content_policy_timeout_or_rate_limit(make_wrapper):
    for err in (litellm.ContextWindowExceededError, litellm.ContentPolicyViolationError, litellm.RateLimitError,
                litellm.Timeout):
        fake = FailsFor(["openai/weird"], err)
        w = make_wrapper(fake, **_fb())
        with pytest.raises(err):
            await w.acomplete([{"role": "user", "content": "hi"}], {}, {})
        assert len(fake.calls) == 1, err.__name__


async def test_fallback_target_is_redacted_according_to_its_own_trust(make_wrapper):
    fake = FailsFor(["openai/weird"], litellm.BadRequestError)
    w = make_wrapper(fake, privacy={"trusted": ["openai/gpt-4o-mini"]}, **_fb())
    await w.acomplete([{"role": "user", "content": "mail jane@acme.com"}], {}, {})
    first, second = fake.calls
    assert "jane@acme.com" not in json.dumps(first["messages"]) and "<EMAIL_" in json.dumps(first["messages"])
    assert "jane@acme.com" in json.dumps(second["messages"])               # the trusted fallback gets the real value
    fake = FailsFor(["openai/weird"], litellm.BadRequestError)
    w = make_wrapper(fake, privacy={"trusted": ["openai/weird"]}, **_fb(to="openai/other"))
    await w.acomplete([{"role": "user", "content": "mail jane@acme.com"}], {}, {})
    assert "jane@acme.com" in json.dumps(fake.calls[0]["messages"])
    assert "jane@acme.com" not in json.dumps(fake.calls[1]["messages"])    # untrusted fallback: redacted


def test_fallback_policy_validation():
    from llm_wrapper.policy import policy_from_dict
    from .conftest import base_policy
    with pytest.raises(Exception, match="wrapper itself"):
        policy_from_dict(base_policy(fallback={"enabled": True, "to": "wrapper/auto"}))
    with pytest.raises(Exception):
        policy_from_dict(base_policy(fallback={"on": ["timeout"]}))
    assert policy_from_dict(base_policy()).policy.fallback.enabled is False


def test_fallback_on_key_written_unquoted_in_yaml(tmp_path):
    from llm_wrapper.policy import load_policy
    f = tmp_path / "p.yaml"
    f.write_text("version: 1\nmodels: {default: m}\nfallback:\n  enabled: true\n  on: [not_found]\n  to: other/model\n")
    fb = load_policy(f).policy.fallback          # YAML 1.1 reads the bare key `on` as the boolean True
    assert fb.enabled and fb.on == ["not_found"] and fb.to == "other/model"
    f.write_text('version: 1\nmodels: {default: m}\nfallback:\n  "on": [auth]\n')
    assert load_policy(f).policy.fallback.on == ["auth"]
    lp = load_policy(tmp_path / "p.yaml")        # the effective policy (what `ab` writes to disk) loads again
    g = tmp_path / "eff.yaml"
    g.write_text(lp.effective_yaml)
    assert load_policy(g).policy.fallback.on == ["auth"]
