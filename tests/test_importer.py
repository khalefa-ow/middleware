import json

from llm_wrapper.analyze import analyze
from llm_wrapper.importers.cppagent import import_traces


def _trace(path):
    ev = [
        {"event": "run.start", "run_id": "1", "seq": 0, "ts": 1.0},
        {"event": "llm.request", "run_id": "1", "task_id": "1", "role": "planner", "attempt": "initial", "seq": 1,
         "ts": 1.1, "model": "deepseek/deepseek-chat", "n": 1, "tools": None,
         "messages": [{"role": "system", "content": "Decide"}, {"role": "user", "content": "mail a@b.co"}]},
        {"event": "llm.response", "run_id": "1", "task_id": "1", "role": "planner", "attempt": "initial", "seq": 2,
         "ts": 1.2, "model": "deepseek/deepseek-chat", "candidate_index": 0, "cached": False, "parse_ok": True,
         "latency_ms": 900, "response_text": "{}", "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        {"event": "llm.error", "run_id": "1", "task_id": "1", "role": "coder", "attempt": "initial", "seq": 3,
         "ts": 1.3, "model": "deepseek/deepseek-chat", "error_type": "BadRequestError", "error": "bad"},
        {"event": "task.finish", "run_id": "1", "task_id": "1", "success": True, "seq": 4, "ts": 1.4},
        {"event": "run.finish", "run_id": "1", "success": False, "seq": 5, "ts": 1.5},
    ]
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(e) for e in ev) + "\n{not json\n")


def test_import_resume_and_analyze(tmp_path, store):
    _trace(tmp_path / "w" / ".cppagent" / "traces" / "run_000001.jsonl")
    s1 = import_traces(str(tmp_path), store)
    assert (s1.files, s1.records, s1.errors, len(s1.bad_lines)) == (1, 2, 1, 1)
    s2 = import_traces(str(tmp_path), store)
    assert s2.skipped_files == 1 and s2.records == 0
    s3 = import_traces(str(tmp_path), store, force=True)  # re-read: rows de-duplicated
    assert s3.records == 2
    rows = store.query("SELECT * FROM requests")
    assert len(rows) == 2
    meta = json.loads(next(r for r in rows if not r["error"])["metadata_json"])
    assert meta["role"] == "planner" and meta["task_success"] is True and meta["run_success"] is False

    rep = analyze(store, "own_import")
    assert rep["by_role"]["planner"]["paid_calls"] == 1 and rep["by_role"]["coder"]["errors"] == 1
    assert rep["pii_hits"] == {"email": 1} and rep["run_success_rate"] == 0.0
