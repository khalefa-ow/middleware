import json

import yaml

from llm_wrapper import experiments as ex
from llm_wrapper.experiments import Dataset
from llm_wrapper.finetune import export_finetune

from .conftest import FakeModel


def _seed(store, n=10):
    for i in range(n):
        msgs = [{"role": "system", "content": "Plan."}, {"role": "user", "content": f"task {i}, mail ann{i}@corp.io"}]
        store.put("requests", {"id": f"r{i:02d}", "ts": i, "source": "own_import", "model_used": "m",
                               "input_json": json.dumps(msgs),
                               "output_json": json.dumps({"role": "assistant", "content": f"ok ann{i}@corp.io"}),
                               "metadata_json": json.dumps({"role": "planner", "task_success": i % 2 == 0})})
    store.flush()


def test_export_redacts_consistently_and_splits(store, tmp_path):
    _seed(store)
    st = export_finetune(store, "ft1", str(tmp_path), Dataset(role="planner"), holdout=0.2)
    assert (st.train, st.holdout) == (8, 2)
    lines = [json.loads(x) for x in open(tmp_path / "ft1.train.jsonl")]
    text = json.dumps(lines)
    assert "@corp.io" not in text
    ex0 = lines[0]["messages"]
    ph = ex0[1]["content"].split("mail ")[1]
    assert ex0[-1] == {"role": "assistant", "content": f"ok {ph}"}   # same placeholder in prompt and answer
    hold = {r["request_id"] for r in store.query("SELECT request_id FROM dataset_splits WHERE split='holdout'")}
    assert len(hold) == 2
    # experiments can be restricted to the holdout
    assert {s["id"] for s in ex.sample(store, Dataset(holdout_of="ft1"))} == hold
    st2 = export_finetune(store, "ft2", str(tmp_path), Dataset(role="planner"), require_success=True, holdout=0)
    assert st2.train == 5 and st2.skipped_no_success == 5


async def test_export_from_experiment_winners(make_wrapper, store, tmp_path):
    _seed(store, 4)

    class Judge(FakeModel):
        async def __call__(self, **kw):
            c = kw["messages"][-1]["content"]
            if "ANSWER A:" in c:  # the strong arm always wins, whichever slot it is in
                a = c.split("ANSWER A:")[1].split("ANSWER B:")[0]
                self.replies = ['{"winner": "%s"}' % ("A" if "STRONG" in a else "B")]
            else:
                self.replies = ["STRONG plan"]
            return await FakeModel.__call__(self, **kw)

    w = make_wrapper(Judge())
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({"id": "e1", "dataset": {"role": "planner"},
                                 "arms": [{"id": "strong", "model": "big"}], "scorers": ["judge"],
                                 "judge": {"model": "j"}}))
    rep = await ex.run_experiment(w, store, str(p))
    assert rep["arms"]["strong"]["judge_score"] == 1.0
    st = export_finetune(store, "distill", str(tmp_path), Dataset(role="planner"), select="experiment:e1:strong",
                         holdout=0)
    rows = [json.loads(x) for x in open(tmp_path / "distill.train.jsonl")]
    assert st.train == 4 and all(r["messages"][-1]["content"] == "STRONG plan" for r in rows)


def test_bootstrap_and_paired():
    assert ex.bootstrap_ci([1.0]) is None
    lo, hi = ex.bootstrap_ci([0.0, 1.0] * 20)
    assert 0.3 < lo < 0.5 < hi < 0.7
    arm = [{"sample_id": i, "ok": 1, "judge": 1.0} for i in range(10)]
    ctl = [{"sample_id": i, "ok": 1, "judge": 0.0} for i in range(10)]
    d = ex._paired(arm, ctl, "judge")
    assert d["n"] == 10 and d["mean"] == 1.0 and d["ci95"][0] > 0
