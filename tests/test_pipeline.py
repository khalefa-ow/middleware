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
