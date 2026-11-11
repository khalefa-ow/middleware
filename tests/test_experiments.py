import json

import yaml

from llm_wrapper import experiments as ex

from .conftest import FakeModel


def _seed(store, n=6):
    for i in range(n):
        msgs = [{"role": "system", "content": "Plan as JSON."}, {"role": "user", "content": f"task {i} for a@b.co"}]
        store.put("requests", {"id": f"r{i}", "ts": i, "source": "own_import", "model_used": "openai/gpt-4o",
                               "input_json": json.dumps(msgs),
                               "output_json": json.dumps({"role": "assistant", "content": '{"steps": [1]}'}),
                               "metadata_json": json.dumps({"role": "planner", "parse_ok": True}),
                               "prompt_tokens": 1000, "completion_tokens": 100, "latency_ms": 900})
    store.put("requests", {"id": "other", "ts": 9, "source": "own_import", "model_used": "openai/gpt-4o",
                           "input_json": "[]", "output_json": "{}", "metadata_json": json.dumps({"role": "coder"})})
    store.flush()


class Scripted(FakeModel):
    """Candidate arms answer JSON (cheap arm drops a key); the judge prefers whichever is A."""

    async def __call__(self, **kw):
        self.calls.append(kw)
        content = kw["messages"][-1]["content"]
        if "ANSWER A:" in content:
            self.replies = ['{"winner": "tie", "reason": "same"}']
        elif kw["model"] == "openai/gpt-4o-mini":
            self.replies = ['```json\n{"steps": [1, 2]}\n```']
        else:
            self.replies = ['{"other": 1}']
        return await FakeModel.__call__(self, **kw)


async def test_experiment_run_report_and_promotion(make_wrapper, store, tmp_path):
    _seed(store)
    fake = Scripted()
    w = make_wrapper(fake)
    spec = {"id": "planner-test", "dataset": {"source": "own_import", "role": "planner", "limit": 5},
            "arms": [{"id": "mini", "model": "openai/gpt-4o-mini", "params": {"temperature": 0}},
                     {"id": "nano", "model": "openai/gpt-4.1-nano", "system_suffix": "Be brief."}],
            "scorers": ["json_valid", "similarity", "judge"], "judge": {"model": "openai/gpt-4.1", "max_usd": 1},
            "budget_usd": 5}
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(spec))
    rep = await ex.run_experiment(w, store, str(p))
    mini, nano = rep["arms"]["mini"], rep["arms"]["nano"]
    assert mini["n"] == 5 and nano["n"] == 5 and mini["errors"] == 0
    assert mini["json_valid"] == 1.0 and nano["json_valid"] == 0.5   # nano misses the "steps" key
    assert mini["judge_score"] == 0.5 and mini["judged"] == 5
    arm_calls = [c for c in fake.calls if "ANSWER A:" not in c["messages"][-1]["content"]]
    assert all("a@b.co" not in json.dumps(c["messages"]) for c in fake.calls)       # redacted, judge included
    assert any("Be brief." in c["messages"][0]["content"] for c in arm_calls if c["model"] == "openai/gpt-4.1-nano")
    assert {c["model"] for c in arm_calls} == {"openai/gpt-4o-mini", "openai/gpt-4.1-nano"}
    # promotion needs >= 20 judged samples by default
    assert "no arm meets" in rep["promotion"]
    rep["control"] = "nano"; rep["arms"]["nano"]["mean_cost_usd"] = 1.0
    snippet = ex.promotion_snippet(rep, "planner", min_judged=5)
    assert "model: openai/gpt-4o-mini" in snippet and "metadata.role == 'planner'" in snippet
    store.flush()
    assert store.query("SELECT count(*) n FROM requests WHERE source='experiment'")[0]["n"] == 20  # 10 arm calls + 10 judge calls
    assert "planner-test" in ex.format_report(ex.report(store, "planner-test"))


def test_scorers():
    assert ex.score_json_valid("plain text", "x") is None
    assert ex.score_json_valid('{"a": 1}', '```json\n{"a": 2, "b": 3}\n```') == 1.0
    assert ex.score_json_valid('{"a": 1}', "not json") == 0.0
    assert ex.score_similarity("abc", "abc") == 1.0


async def test_replay_uses_recorded_params(make_wrapper, store, tmp_path):
    store.put("requests", {"id": "live1", "ts": 1, "source": "own_live", "model_used": "openai/gpt-4o",
                           "input_json": json.dumps([{"role": "user", "content": "x"}]),
                           "output_json": json.dumps({"role": "assistant", "content": "y"}),
                           "params_json": json.dumps({"temperature": 0.2, "max_tokens": 50, "response_format": True}),
                           "metadata_json": "{}"})
    store.flush()
    fake = FakeModel()
    w = make_wrapper(fake)
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({"id": "e", "dataset": {"source": "own_live"},
                                 "arms": [{"id": "a", "model": "m1", "params": {"max_tokens": 99}}]}))
    await ex.run_experiment(w, store, str(p))
    sent = fake.calls[0]
    assert sent["temperature"] == 0.2 and sent["max_tokens"] == 99 and "response_format" not in sent


def test_sample_skips_experiment_rows(store):
    _seed(store, 2)
    store.put("requests", {"id": "judge1", "ts": 5, "source": "experiment", "model_used": "j",
                           "input_json": json.dumps([{"role": "user", "content": "ANSWER A: ..."}]),
                           "output_json": json.dumps({"role": "assistant", "content": "{}"}),
                           "metadata_json": json.dumps({"role": "planner", "experiment": "e"})})
    store.flush()
    assert {s["id"] for s in ex.sample(store, ex.Dataset(role="planner"))} == {"r0", "r1"}
    assert {s["id"] for s in ex.sample(store, ex.Dataset(source="experiment"))} == {"judge1"}


async def test_budget_exhausted_is_recorded_not_silent(make_wrapper, store, tmp_path):
    _seed(store, 3)
    fake = Scripted()
    w = make_wrapper(fake)
    p = tmp_path / "b.yaml"
    p.write_text(yaml.safe_dump({"id": "b0", "dataset": {"role": "planner"}, "budget_usd": 0,
                                 "arms": [{"id": "mini", "model": "openai/gpt-4o-mini"}]}))
    rep = await ex.run_experiment(w, store, str(p))
    assert fake.calls == []
    a = rep["arms"]["mini"]
    assert (a["n"], a["skipped_budget"], a["errors"]) == (3, 3, 0)
    assert "skipped" in ex.format_report(rep)
    rows = store.query("SELECT error, ok FROM experiment_results WHERE experiment_id='b0'")
    assert {(r["error"], r["ok"]) for r in rows} == {("budget exhausted", 0)}


async def test_promotion_refuses_unsafe_role(make_wrapper, store, tmp_path):
    _seed(store)
    w = make_wrapper(Scripted())
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({"id": "safe", "dataset": {"role": "planner"},
                                 "arms": [{"id": "a", "model": "openai/gpt-4o-mini"},
                                          {"id": "b", "model": "openai/gpt-4o-mini"}]}))
    await ex.run_experiment(w, store, str(p))
    rep = ex.report(store, "safe")
    rep["arms"]["b"] |= {"judged": 50, "judge_score": 0.9, "judge_ci95": [0.8, 1.0], "mean_cost_usd": 0.1,
                         "json_valid": 1.0, "vs_control": {}}
    rep["arms"]["a"]["mean_cost_usd"] = 1.0
    assert "routes:" in ex.promotion_snippet(rep, "planner")
    out = ex.promotion_snippet(rep, "it's")
    assert "routes:" not in out and "by hand" in out


async def test_replay_forwards_recorded_schema_and_arm_null_removes_it(make_wrapper, store, tmp_path):
    schema = {"type": "json_schema", "json_schema": {"name": "plan", "schema": {"type": "object"}}}
    store.put("requests", {"id": "live2", "ts": 1, "source": "own_live", "model_used": "openai/gpt-4o",
                           "input_json": json.dumps([{"role": "user", "content": "x"}]),
                           "output_json": json.dumps({"role": "assistant", "content": "y"}),
                           "params_json": json.dumps({"response_format": schema}), "metadata_json": "{}"})
    store.flush()
    fake = FakeModel()
    w = make_wrapper(fake)
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({"id": "e2", "dataset": {"source": "own_live"},
                                 "arms": [{"id": "keep", "model": "m1"},
                                          {"id": "drop", "model": "m2", "params": {"response_format": None}}]}))
    await ex.run_experiment(w, store, str(p))
    by_model = {c["model"]: c for c in fake.calls}
    assert by_model["m1"]["response_format"] == schema
    assert "response_format" not in by_model["m2"]


def test_dry_run_calls_nothing_writes_nothing_and_reports_estimates(make_wrapper, store, tmp_path):
    _seed(store, 3)
    store.put("requests", {"id": "huge", "ts": 20, "source": "own_import", "model_used": "openai/gpt-4o",
                           "input_json": json.dumps([{"role": "user", "content": "word " * 3000}]),
                           "output_json": json.dumps({"role": "assistant", "content": "{}"}),
                           "metadata_json": json.dumps({"role": "planner", "parse_ok": True})})
    store.flush()
    fake = FakeModel()
    w = make_wrapper(fake, size={"limits": {"openai/gpt-4o-mini": 1000}, "reserve_output_tokens": 100})
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump({"id": "dry", "dataset": {"role": "planner"}, "scorers": ["judge"],
                                 "judge": {"model": "j", "max_usd": 0.25},
                                 "arms": [{"id": "mini", "model": "openai/gpt-4o-mini"}]}))
    d = ex.dry_run(w, store, str(p))
    a = d["arms"]["mini"]
    assert fake.calls == []
    assert a["samples"] == 3 and a["would_fail"] == {"ContextTooLarge": 1}
    assert a["mean_input_tokens"] > 0 and a["mean_cost_usd"] > 0 and a["mean_placeholders"] >= 1
    assert d["judge_max_usd"] == 0.25 and "estimates" in ex.format_dry_run(d)
    store.flush()
    assert store.query("SELECT count(*) AS n FROM requests WHERE source='experiment'")[0]["n"] == 0
    tables = {r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("experiment_results", "experiments"):  # not even created by a dry run
        assert t not in tables or store.query(f"SELECT count(*) AS n FROM {t}")[0]["n"] == 0


SCORER_MODULE = '''
def has_steps(sample, answer):
    assert sample["messages"] and "reference" in sample and "meta" in sample and "params" in sample
    return 1.0 if '"steps"' in answer else 0.0

def boom(sample, answer):
    raise RuntimeError("scorer exploded")

def out_of_range(sample, answer):
    return 3
'''


async def test_scorer_plugins_scores_ci_paired_and_primary_promotion(make_wrapper, store, tmp_path):
    _seed(store, 25)
    (tmp_path / "plug_scorers_t9.py").write_text(SCORER_MODULE)
    w = make_wrapper(Scripted())
    spec = {"id": "plug", "dataset": {"role": "planner", "limit": 25}, "primary": "has_steps",
            "scorers": ["json_valid", "python:plug_scorers_t9:has_steps", "python:plug_scorers_t9:boom",
                        "python:plug_scorers_t9:out_of_range"],
            "arms": [{"id": "ctl", "model": "openai/gpt-4o"}, {"id": "mini", "model": "openai/gpt-4o-mini"}]}
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(spec))
    rep = await ex.run_experiment(w, store, str(p))
    mini, ctl = rep["arms"]["mini"], rep["arms"]["ctl"]
    assert mini["plugins"]["has_steps"]["mean"] == 1.0 and ctl["plugins"]["has_steps"]["mean"] == 0.0
    assert mini["plugins"]["has_steps"]["ci95"] is not None and mini["plugins"]["has_steps"]["n"] == 25
    pv = mini["vs_control"]["has_steps"]
    assert pv["mean"] == 1.0 and pv["n"] == 25 and pv["ci95"][0] > 0
    # a raising scorer and an out-of-range one yield no score, never abort, and the errors are reported
    assert mini["plugins"]["boom"]["n"] == 0 and "scorer exploded" in mini["plugins"]["boom"]["errors"][0]
    assert "expected a number in 0..1" in mini["plugins"]["out_of_range"]["errors"][0]
    assert "has_steps" in ex.format_report(rep) and "boom error" in ex.format_report(rep)
    stored = json.loads(store.query("SELECT scores_json FROM experiment_results WHERE arm_id='mini'")[0]["scores_json"])
    assert stored["has_steps"] == 1.0 and stored["boom"] is None and "boom" in stored["_errors"]
    # promotion is decided by the plugin primary scorer (no judge configured at all)
    assert "routes:" in rep["promotion"] and "has_steps 1.00 over 25" in rep["promotion"]
    # the report also works from the stored rows alone
    assert ex.report(store, "plug")["arms"]["mini"]["plugins"]["has_steps"]["mean"] == 1.0


def test_scorer_spec_validation():
    import pytest
    base = {"id": "x", "arms": [{"id": "a", "model": "m"}]}
    with pytest.raises(ValueError, match="unique"):
        ex.Spec.model_validate(base | {"scorers": ["python:a:f", "python:b:f"]})
    with pytest.raises(ValueError, match="built-in"):
        ex.Spec.model_validate(base | {"scorers": ["python:a:judge"]})
    with pytest.raises(ValueError, match="unknown scorer"):
        ex.Spec.model_validate(base | {"scorers": ["nonsense"]})
    with pytest.raises(ValueError, match="primary"):
        ex.Spec.model_validate(base | {"scorers": ["similarity"], "primary": "other"})
    ex.Spec.model_validate(base | {"scorers": ["similarity"]})  # default primary judge, no judge: allowed


async def test_scorer_plugin_load_failure_is_a_clear_error(make_wrapper, store, tmp_path):
    import pytest
    _seed(store, 2)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump({"id": "bad", "arms": [{"id": "a", "model": "m"}],
                                 "scorers": ["python:no_such_module_t9:f"]}))
    with pytest.raises(ValueError, match="cannot load scorer"):
        await ex.run_experiment(make_wrapper(FakeModel()), store, str(p))


async def test_limit_override_and_exploratory_label(make_wrapper, store, tmp_path):
    _seed(store, 6)
    fake = Scripted()
    w = make_wrapper(fake)
    p = tmp_path / "l.yaml"
    p.write_text(yaml.safe_dump({"id": "lim", "dataset": {"role": "planner", "limit": 5},
                                 "arms": [{"id": "mini", "model": "openai/gpt-4o-mini"}],
                                 "scorers": ["judge"], "judge": {"model": "j", "max_usd": 1}}))
    rep = await ex.run_experiment(w, store, str(p), limit=2)
    assert rep["arms"]["mini"]["n"] == 2
    assert "exploratory: fewer than 100 scored samples" in ex.format_report(rep)
    assert ex.dry_run(w, store, str(p), limit=3)["samples"] == 3
    import pytest
    with pytest.raises(ValueError, match="at least 1"):
        ex.load_spec(str(p), limit=0)
