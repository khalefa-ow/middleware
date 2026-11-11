import json
import sys
from pathlib import Path

import pytest
import yaml

from llm_wrapper import ab
from llm_wrapper.storage import Store

ROOT = Path(__file__).resolve().parent.parent
SECRET = "zz9-ab-secret-value"
KEY_HEX = "ab" * 32


def _write(tmp_path, arms, runs=2, **over):
    (tmp_path / "base.yaml").write_text(yaml.safe_dump({
        "version": 1, "models": {"default": "openai/gpt-4o-mini"},
        "privacy": {"custom_patterns": [{"id": "tok", "type": "TOKEN", "regex": "${ENV:AB_SECRET_T12}"}]}}))
    spec = {"id": "abt", "command": [sys.executable, "-m", "tests.ab_child"], "cwd": str(ROOT),
            "env": {"WRAPPER_PRIVACY_KEY": KEY_HEX},  # so the child never writes a key file in the home folder
            "arms": [{"id": a["id"], "policy": "base.yaml", "env": a.get("env", {})} for a in arms],
            "runs": runs, "out_dir": "out", "timeout_s": 60} | over
    p = tmp_path / "ab.yaml"
    p.write_text(yaml.safe_dump(spec))
    return str(p)


@pytest.fixture
def ab_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AB_SECRET_T12", SECRET)
    monkeypatch.setenv("WRAPPER_PRIVACY_KEY", KEY_HEX)
    s = Store(str(tmp_path / "ab.db"))
    yield s
    s.close()


def test_ab_run_records_runs_tags_calls_and_never_writes_secrets(ab_store, tmp_path):
    p = _write(tmp_path, [{"id": "good", "env": {"CHILD_EXIT": "0"}}, {"id": "bad", "env": {"CHILD_EXIT": "1"}}])
    rep = ab.run_ab(ab_store, p, log=lambda m: None)
    rows = [dict(r) for r in ab_store.query("SELECT * FROM ab_runs ORDER BY arm_id, run_index")]
    assert len(rows) == 4
    assert [(r["arm_id"], r["success"]) for r in rows] == [("bad", 0), ("bad", 0), ("good", 1), ("good", 1)]
    for r in rows:
        assert r["calls"] == 2 and r["cost_usd"] > 0 and r["errors"] == 0 and not r["skipped"]
        tags = {x["run_tag"] for x in ab_store.query("SELECT run_tag FROM requests WHERE run_tag=?", (r["tag"],))}
        assert tags == {r["tag"]}
    assert rep["arms"]["good"]["successes"] == 2 and rep["arms"]["bad"]["successes"] == 0
    assert rep["arms"]["bad"]["vs_control"]["success_rate_diff"] == -1.0
    text = ab.format_ab_report(rep)
    assert "100%" in text and "0%" in text and "few runs" in text
    # the policy written for an arm keeps the reference, not the secret value
    written = (tmp_path / "out" / "abt" / "good.policy.yaml").read_text()
    assert "${ENV:AB_SECRET_T12}" in written and SECRET not in written
    for f in (tmp_path / "out").rglob("*.log"):
        assert SECRET not in f.read_text()


def test_ab_budget_stop_marks_remaining_runs_skipped(ab_store, tmp_path):
    p = _write(tmp_path, [{"id": "a", "env": {"CHILD_EXIT": "0"}}], runs=3, budget_usd=1e-9)
    rep = ab.run_ab(ab_store, p, log=lambda m: None)
    rows = [dict(r) for r in ab_store.query("SELECT * FROM ab_runs ORDER BY run_index")]
    assert [r["skipped"] for r in rows] == [0, 1, 1]
    a = rep["arms"]["a"]
    assert (a["runs"], a["skipped"], a["successes"]) == (1, 2, 1)   # skipped runs are not failures
    assert "skipped" in ab.format_ab_report(rep)


def test_ab_stdout_regex_success_rule_and_rerun_replaces_rows(ab_store, tmp_path):
    arms = [{"id": "t", "env": {"CHILD_PRINT": "Success: True"}}, {"id": "f", "env": {"CHILD_PRINT": "Success: False"}}]
    p = _write(tmp_path, arms, runs=1, success={"stdout_regex": "^Success: True"})
    rep = ab.run_ab(ab_store, p, log=lambda m: None)
    assert (rep["arms"]["t"]["successes"], rep["arms"]["f"]["successes"]) == (1, 0)
    ab.run_ab(ab_store, p, log=lambda m: None)   # same id again: earlier rows are replaced, not added
    assert ab_store.query("SELECT count(*) AS n FROM ab_runs")[0]["n"] == 2


def test_ab_timeout_counts_as_failed_run_and_kills_the_command(ab_store, tmp_path):
    p = _write(tmp_path, [{"id": "slow"}], runs=1, timeout_s=0.5,
               command=[sys.executable, "-c", "import time; time.sleep(30)"])
    rep = ab.run_ab(ab_store, p, log=lambda m: None)
    row = dict(ab_store.query("SELECT * FROM ab_runs")[0])
    assert row["timed_out"] == 1 and row["success"] == 0 and row["duration_s"] < 10
    assert rep["arms"]["slow"]["timeouts"] == 1


def test_ab_argument_placeholders_and_spec_validation(ab_store, tmp_path):
    p = _write(tmp_path, [{"id": "x"}], runs=1,
               command=[sys.executable, "-c",
                        "import sys; open(sys.argv[1] + '/seen.txt', 'w').write(' '.join(sys.argv[2:]))",
                        "{run_dir}", "{arm}", "{i}", "{policy}"])
    ab.run_ab(ab_store, p, log=lambda m: None)
    seen = (tmp_path / "out" / "abt" / "x" / "0" / "seen.txt").read_text().split()
    assert seen[0] == "x" and seen[1] == "0" and seen[2].endswith("x.policy.yaml")
    with pytest.raises(ValueError, match="exactly one"):
        ab.ABSpec.model_validate({"id": "a", "command": ["x"], "arms": [{"id": "c", "policy": "p"}],
                                  "success": {"exit_code": 0, "stdout_regex": "x"}})
    with pytest.raises(ValueError, match="unique"):
        ab.ABSpec.model_validate({"id": "a", "command": ["x"],
                                  "arms": [{"id": "c", "policy": "p"}, {"id": "c", "policy": "p"}]})


def test_wilson_and_bootstrap_diff():
    lo, hi = ab.wilson(5, 5)
    assert 0.5 < lo < 1.0 and hi == pytest.approx(1.0)
    assert ab.wilson(0, 0) is None
    ci = ab.bootstrap_diff([1.0] * 10, [0.0] * 10)
    assert ci == (1.0, 1.0)
    lo, hi = ab.bootstrap_diff([1.0, 0.0] * 5, [1.0, 0.0] * 5)
    assert lo < 0 < hi
    assert ab.bootstrap_diff([], [1.0]) is None


def test_example_cppagent_spec_and_policies_load(monkeypatch):
    from llm_wrapper.policy import load_policy
    spec, _ = ab.load_ab_spec(str(ROOT / "examples" / "cppagent_ab" / "ab.yaml"))
    base = ROOT / "examples" / "cppagent_ab"
    models = []
    for arm in spec.arms:
        lp = load_policy(base / arm.policy, [base / o for o in arm.overlays])
        models.append(lp.policy.models.default)
    assert models == ["deepseek/deepseek-chat", "openai/gpt-6-luna"]
    assert spec.success.stdout_regex == "^Success: True" and spec.arms[0].id == "deepseek"


def test_ab_report_on_a_database_without_ab_tables_is_a_clear_error(tmp_path, capsys):
    from llm_wrapper.cli import main
    s = Store(str(tmp_path / "empty.db"))
    with pytest.raises(ValueError, match="no A/B experiment named 'nope'"):
        ab.ab_report(s, "nope")
    s.close()
    assert main(["--db", str(tmp_path / "empty.db"), "ab", "report", "nope"]) == 1
    assert "no A/B experiment named" in capsys.readouterr().err
