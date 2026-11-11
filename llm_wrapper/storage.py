"""Local SQLite store with one background writer thread.

Requests never wait on DB writes: they enqueue rows and the writer thread commits them in
batches (WAL mode). Reads use their own short-lived connection. Real values are stored
locally; keep the file on an encrypted disk (it is created 0600).
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, ts REAL, source TEXT, session_key TEXT, user TEXT,
  model_requested TEXT, model_used TEXT, route_id TEXT, policy_hash TEXT,
  fast_path INTEGER, stream INTEGER, input_json TEXT, output_json TEXT, params_json TEXT,
  metadata_json TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL,
  latency_ms REAL, finish_reason TEXT, error TEXT, content_hash TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_src ON requests(source);
CREATE UNIQUE INDEX IF NOT EXISTS requests_hash ON requests(content_hash) WHERE content_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS spans (
  id TEXT PRIMARY KEY, request_id TEXT, parent_id TEXT, name TEXT, start REAL, end REAL,
  status TEXT, attrs_json TEXT
);
CREATE INDEX IF NOT EXISTS spans_req ON spans(request_id);
CREATE TABLE IF NOT EXISTS egress_events (
  request_id TEXT, span_id TEXT, target TEXT, trusted INTEGER, placeholders INTEGER,
  non_text_parts INTEGER, leak_blocked INTEGER, ts REAL
);
CREATE TABLE IF NOT EXISTS redactions (request_id TEXT, pattern_id TEXT, placeholder TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS size_events (
  request_id TEXT, model TEXT, tokens INTEGER, limit_tokens INTEGER, strategy TEXT,
  success INTEGER, detail TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS vault (
  placeholder TEXT, scope_key TEXT, value TEXT, value_hash TEXT, type TEXT, origin TEXT,
  key_version INTEGER, created REAL, PRIMARY KEY (placeholder, scope_key)
);
CREATE TABLE IF NOT EXISTS vault_refs (value_hash TEXT, request_id TEXT, PRIMARY KEY (value_hash, request_id));
CREATE TABLE IF NOT EXISTS policy_versions (hash TEXT PRIMARY KEY, effective_yaml TEXT, source TEXT, loaded_at REAL);
CREATE TABLE IF NOT EXISTS import_batches (
  id TEXT PRIMARY KEY, source TEXT, path TEXT, fingerprint TEXT, records INTEGER, skipped INTEGER,
  imported_at REAL
);
CREATE INDEX IF NOT EXISTS import_path ON import_batches(path, fingerprint);
"""


MIGRATIONS = {"requests": {"cached_tokens": "INTEGER"}}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created."""
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def connect(path: str) -> sqlite3.Connection:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    conn = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
    if new:
        os.chmod(p, 0o600)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class Store:
    """SQLite store; writes go through a background thread (``put``), reads are direct."""

    def __init__(self, path: str):
        self.path = path
        with contextlib.closing(connect(path)) as c, c:
            c.executescript(SCHEMA)
            _migrate(c)
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wrapper-db-writer", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    # -- writes
    def put(self, table: str, row: dict[str, Any], mode: str = "INSERT OR IGNORE") -> None:
        self._q.put((table, row, mode))

    def _run(self) -> None:
        conn = connect(self.path)
        while not (self._stop.is_set() and self._q.empty()):
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            batch = [item]
            while len(batch) < 500:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            try:
                with conn:
                    for table, row, mode in batch:
                        cols = ",".join(row)
                        conn.execute(f"{mode} INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})",
                                     list(row.values()))
            except sqlite3.Error as exc:  # never crash the writer; report and continue
                print(f"[llm_wrapper] db write failed: {exc}", flush=True)
            finally:
                for _ in batch:
                    self._q.task_done()
        conn.close()

    def flush(self, timeout: float = 30) -> None:
        end = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.01)

    def close(self) -> None:
        if self._stop.is_set():
            return
        self.flush()
        self._stop.set()
        self._thread.join(timeout=5)

    def queue_depth(self) -> int:
        return self._q.qsize()

    # -- reads / maintenance (synchronous)
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with contextlib.closing(connect(self.path)) as c:
            return c.execute(sql, list(params)).fetchall()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        self.flush()
        conn = connect(self.path)
        try:
            with conn:
                return conn.execute(sql, list(params)).rowcount
        finally:
            conn.close()

    def delete_request(self, request_id: str) -> int:
        n = 0
        for sql in ("DELETE FROM spans WHERE request_id=?", "DELETE FROM egress_events WHERE request_id=?",
                    "DELETE FROM redactions WHERE request_id=?", "DELETE FROM size_events WHERE request_id=?",
                    "DELETE FROM vault_refs WHERE request_id=?", "DELETE FROM requests WHERE id=?"):
            n += self.execute(sql, (request_id,))
        return n

    def requests_with_value(self, value_hash: str) -> list[str]:
        return [r["request_id"] for r in self.query(
            "SELECT request_id FROM vault_refs WHERE value_hash=?", (value_hash,))]

    def purge(self, requests_days: int, vault_days: int) -> dict[str, int]:
        now = time.time()
        old = [r["id"] for r in self.query("SELECT id FROM requests WHERE ts < ?", (now - requests_days * 86400,))]
        for rid in old:
            self.delete_request(rid)
        v = self.execute("DELETE FROM vault WHERE created < ?", (now - vault_days * 86400,))
        return {"requests": len(old), "vault_entries": v}
