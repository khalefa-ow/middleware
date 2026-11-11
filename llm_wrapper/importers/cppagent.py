"""Import cppagent trace files (``.cppagent/traces/run_*.jsonl``).

Each ``llm.response`` becomes one request row (source ``own_import``) carrying the messages
of the matching ``llm.request``, the response text, usage, latency, the caller's role,
and the outcome of its task and run. ``llm.error`` events become rows with ``error`` set.
Malformed lines are skipped and counted. A file whose size and mtime are unchanged since its
last import is skipped (resume); re-imported rows are de-duplicated by content hash.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..storage import Store, dumps


@dataclass
class ImportStats:
    files: int = 0
    skipped_files: int = 0
    records: int = 0
    errors: int = 0
    bad_lines: list[str] = field(default_factory=list)


def find_trace_files(root: str) -> list[str]:
    p = Path(root).expanduser()
    if p.is_file():
        return [str(p)]
    return sorted(glob.glob(str(p / "**" / ".cppagent" / "traces" / "*.jsonl"), recursive=True))


def _fingerprint(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"


def _rows(path: str, stats: ImportStats):
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                stats.bad_lines.append(f"{path}:{n}")


def import_file(path: str, store: Store, stats: ImportStats, force: bool = False) -> int:
    fp = _fingerprint(path)
    if not force and store.query("SELECT 1 FROM import_batches WHERE path=? AND fingerprint=?", (path, fp)):
        stats.skipped_files += 1
        return 0
    events = list(_rows(path, stats))
    run_ok: dict[str, bool] = {}
    task_ok: dict[tuple[str, str], bool] = {}
    for e in events:
        if e.get("event") == "run.finish":
            run_ok[e.get("run_id")] = bool(e.get("success"))
        elif e.get("event") == "task.finish":
            task_ok[(e.get("run_id"), e.get("task_id"))] = bool(e.get("success"))

    session = f"cppagent:{Path(path).parent.parent.parent}:{Path(path).stem}"
    last_req: dict[tuple, dict] = {}
    count = 0
    for e in events:
        ev = e.get("event")
        if ev == "llm.request":
            last_req[(e.get("task_id"), e.get("role"), e.get("attempt"))] = e
            last_req[(e.get("task_id"), e.get("role"))] = e
            continue
        if ev not in ("llm.response", "llm.error"):
            continue
        req = last_req.get((e.get("task_id"), e.get("role"), e.get("attempt"))) or \
            last_req.get((e.get("task_id"), e.get("role")))
        messages = req.get("messages") if req else None
        usage = e.get("usage") or {}
        text = e.get("response_text")
        meta = {
            "role": e.get("role"), "task_id": e.get("task_id"), "run_id": e.get("run_id"),
            "attempt": e.get("attempt"), "candidate_index": e.get("candidate_index"),
            "cached": bool(e.get("cached")), "parse_ok": e.get("parse_ok"), "edited": e.get("edited"),
            "task_success": task_ok.get((e.get("run_id"), e.get("task_id"))),
            "run_success": run_ok.get(e.get("run_id")), "trace_file": path, "seq": e.get("seq"),
            "has_tools": bool(req and req.get("tools")), "n": req.get("n") if req else None,
            "error_type": e.get("error_type"), "error_stage": e.get("stage"),
        }
        chash = hashlib.sha256(f"{path}\x00{e.get('run_id')}\x00{e.get('seq')}".encode()).hexdigest()
        store.put("requests", {
            "id": uuid.uuid4().hex, "ts": e.get("ts"), "source": "own_import", "session_key": session,
            "user": None, "model_requested": e.get("model"), "model_used": e.get("model"),
            "route_id": e.get("role"), "policy_hash": None, "fast_path": None, "stream": 0,
            "input_json": dumps(messages) if messages is not None else None,
            "output_json": dumps({"role": "assistant", "content": text, "tool_calls": e.get("tool_calls")})
            if ev == "llm.response" else None,
            "params_json": None, "metadata_json": dumps(meta),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "cost_usd": None, "latency_ms": e.get("latency_ms"),
            "finish_reason": None, "error": e.get("error") if ev == "llm.error" else None,
            "content_hash": chash,
        })
        count += 1
        stats.errors += ev == "llm.error"
    store.put("import_batches", {"id": uuid.uuid4().hex, "source": "cppagent", "path": path, "fingerprint": fp,
                                 "records": count, "skipped": len(stats.bad_lines), "imported_at": time.time()})
    return count


def import_traces(root: str, store: Store, force: bool = False) -> ImportStats:
    stats = ImportStats()
    for f in find_trace_files(root):
        stats.files += 1
        stats.records += import_file(f, store, stats, force)
    store.flush()
    return stats
