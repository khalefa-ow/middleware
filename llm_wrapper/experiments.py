"""Experiments: replay recorded calls under variants and measure cost, latency and quality.

This is the platform's tuning loop:  record (gateway) -> experiment (replay variants)
-> compare (scorers) -> promote (a policy route you approve).

A spec (YAML) names a sample of recorded calls and a set of arms. Each arm is a model plus
optional params and a system-prompt suffix. Every arm call goes through the gateway pipeline,
so redaction, size checks and tracing apply exactly as in production. Answers are scored
against the logged answer:
  json_valid  - the answer parses as JSON whenever the logged answer did (structured calls)
  similarity  - character similarity to the logged answer (cheap drift signal, 0..1)
  judge       - a stronger model compares logged vs. candidate blind, in random order,
                and returns win / tie / loss for the candidate (capped by judge.max_usd)

Example spec:
    id: planner-cheaper
    dataset: {source: own_import, role: planner, limit: 20, seed: 1}
    arms:
      - {id: deepseek, model: deepseek/deepseek-chat, params: {temperature: 0}}
      - {id: mini, model: openai/gpt-4.1-mini}
    scorers: [json_valid, similarity, judge]
    judge: {model: openai/gpt-4.1, max_usd: 0.50}
    budget_usd: 1.00
    concurrency: 4

Replays reuse the caller's recorded params, including a recorded ``response_format`` schema.
An arm param whose value is ``null`` removes that param (e.g. ``params: {response_format: null}``).

Budgets: ``budget_usd`` caps the arm calls only; the judge has its own cap (``judge.max_usd``).
Calls already in flight when a cap is reached still finish, so spend can overshoot by up to
``concurrency`` calls. Samples not run because the budget was spent get a result row with
``error = "budget exhausted"`` and show up as ``skipped_budget`` in the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import difflib
import json
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .pipeline import Wrapper
from .storage import Store, dumps


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


_PLUGIN = re.compile(r"^python:([A-Za-z_][\w.]*):([A-Za-z_]\w*)$")


class Dataset(_Strict):
    source: str | None = None
    role: str | None = None
    model: str | None = None
    limit: int = 20
    seed: int = 1
    only_ok: bool = True
    holdout_of: str | None = None
    """Only use the holdout split of this fine-tune export (keeps training rows out of the eval)."""


class Arm(_Strict):
    id: str
    model: str
    params: dict[str, Any] = {}
    system: str | None = None
    """Replaces the content of the first system message (adds one if the prompt has none)."""
    system_file: str | None = None
    """Same, read from a file (relative to the spec). Not together with ``system``."""
    system_suffix: str | None = None
    """Appended to the system message, after any replacement."""
    transform: str | None = None
    """``python:<module>:<function>``: ``fn(messages) -> messages``, applied last (e.g. trim old turns)."""

    @model_validator(mode="after")
    def _check_prompt(self) -> "Arm":
        if self.system is not None and self.system_file is not None:
            raise ValueError(f"arm {self.id!r}: use either system or system_file, not both")
        if self.transform is not None and not _PLUGIN.match(self.transform):
            raise ValueError(f"arm {self.id!r}: transform must look like python:<module>:<function>")
        return self


class Judge(_Strict):
    model: str
    params: dict[str, Any] = {}
    """Judge call params; empty by default because reasoning models reject temperature=0."""
    max_usd: float = 0.5
    max_chars: int = 12000


BUILTIN_SCORERS = ("json_valid", "similarity", "judge")


def scorer_name(ref: str) -> str:
    """Built-ins keep their name; a plugin ``python:module:function`` is named after its function."""
    m = _PLUGIN.match(ref)
    return m.group(2) if m else ref


class Spec(_Strict):
    id: str
    dataset: Dataset = Dataset()
    arms: list[Arm] = Field(min_length=1)
    scorers: list[str] = ["json_valid", "similarity"]
    """Built-ins (json_valid, similarity, judge) and/or plugins written ``python:<module>:<function>``."""
    primary: str = "judge"
    """The scorer the promotion bar uses: ``judge`` or a plugin's function name."""
    judge: Judge | None = None
    budget_usd: float = 1.0
    concurrency: int = Field(4, ge=1, le=32)

    @model_validator(mode="after")
    def _check_scorers(self) -> "Spec":
        names = []
        for ref in self.scorers:
            if ref in BUILTIN_SCORERS:
                names.append(ref)
            elif m := _PLUGIN.match(ref):
                if m.group(2) in BUILTIN_SCORERS:
                    raise ValueError(f"plugin {ref!r} uses the built-in scorer name {m.group(2)!r}")
                names.append(m.group(2))
            else:
                raise ValueError(f"unknown scorer {ref!r}: use {', '.join(BUILTIN_SCORERS)} or python:<module>:<function>")
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"scorer names must be unique, repeated: {', '.join(dup)}")
        if self.primary not in names and not (self.primary == "judge" and "judge" not in names):
            raise ValueError(f"primary scorer {self.primary!r} is not in scorers {names}")
        return self

    @property
    def plugin_refs(self) -> list[str]:
        return [r for r in self.scorers if r not in BUILTIN_SCORERS]


BUDGET_ERROR = "budget exhausted"
MIN_DECISION_SAMPLES = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, spec_yaml TEXT, started REAL, finished REAL, status TEXT);
CREATE TABLE IF NOT EXISTS experiment_results (
  experiment_id TEXT, arm_id TEXT, sample_id TEXT, model TEXT, ok INTEGER, error TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER, cost_usd REAL, latency_ms REAL,
  answer TEXT, json_valid REAL, similarity REAL, judge REAL, judge_cost_usd REAL, judge_error TEXT, ts REAL,
  scores_json TEXT
);
CREATE INDEX IF NOT EXISTS exp_res ON experiment_results(experiment_id, arm_id);
CREATE TABLE IF NOT EXISTS dataset_splits (name TEXT, request_id TEXT, split TEXT, PRIMARY KEY (name, request_id));
"""


RESULT_COLUMNS = {"judge_error": "TEXT", "scores_json": "TEXT"}
"""Columns added after the first release; ``ensure_schema`` adds them to older databases."""


def ensure_schema(store: Store) -> None:
    with store_conn(store) as c:
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(experiment_results)")}
        for col, typ in RESULT_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE experiment_results ADD COLUMN {col} {typ}")


def _import_callable(ref: str, spec_dir: str) -> Any:
    """Import ``python:module:function``. The spec's folder goes first on sys.path.
    Specs run code: only load specs you trust."""
    import importlib
    import sys

    _, mod, fn = ref.split(":")
    d = str(Path(spec_dir).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    importlib.invalidate_caches()
    loaded = sys.modules.get(mod)
    if loaded is not None and not str(getattr(loaded, "__file__", "") or "").startswith(d):
        del sys.modules[mod]  # same module name from another folder: load this spec's own
    try:
        f = getattr(importlib.import_module(mod), fn)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"cannot load {ref!r}: {exc}") from exc
    if not callable(f):
        raise ValueError(f"{ref!r} is not callable")
    return f


def load_plugins(spec: Spec, spec_dir: str) -> dict[str, Any]:
    """Scorer plugins by name (the function's name)."""
    try:
        return {scorer_name(r): _import_callable(r, spec_dir) for r in spec.plugin_refs}
    except ValueError as exc:
        raise ValueError(str(exc).replace("cannot load ", "cannot load scorer ", 1)) from exc


class ReplayContext:
    """Per-spec things an arm needs to build its prompt: system text from files and transform functions."""

    def __init__(self, spec: Spec, spec_dir: str):
        self.systems: dict[str, str] = {}
        self.transforms: dict[str, Any] = {}
        for arm in spec.arms:
            if arm.system_file is not None:
                path = Path(spec_dir) / arm.system_file
                try:
                    self.systems[arm.id] = path.read_text()
                except OSError as exc:
                    raise ValueError(f"arm {arm.id!r}: cannot read system_file {path}: {exc}") from exc
            elif arm.system is not None:
                self.systems[arm.id] = arm.system
            if arm.transform:
                self.transforms[arm.id] = _import_callable(arm.transform, spec_dir)


def load_spec(path: str, limit: int | None = None) -> tuple[Spec, str]:
    """``limit`` overrides ``dataset.limit`` (the --limit flag)."""
    text = open(path).read()
    spec = Spec.model_validate(yaml.safe_load(text))
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be at least 1")
        spec.dataset.limit = limit
    return spec, text


def sample(store: Store, ds: Dataset) -> list[dict[str, Any]]:
    rows = store.query("SELECT id, model_used, input_json, output_json, metadata_json, params_json, prompt_tokens, "
                       "completion_tokens, cost_usd, latency_ms, source FROM requests "
                       "WHERE input_json IS NOT NULL AND output_json IS NOT NULL AND error IS NULL")
    allowed = None
    if ds.holdout_of:
        with store_conn(store) as c:
            c.executescript(SCHEMA)
        allowed = {r["request_id"] for r in store.query(
            "SELECT request_id FROM dataset_splits WHERE name=? AND split='holdout'", (ds.holdout_of,))}
        if not allowed:
            raise ValueError(f"no holdout split named {ds.holdout_of!r} (run export-finetune first)")
    out = []
    for r in rows:
        if allowed is not None and r["id"] not in allowed:
            continue
        meta = json.loads(r["metadata_json"] or "{}")
        if meta.get("delta_input") or meta.get("cached") or meta.get("candidate_index") not in (0, None):
            continue  # need full prompts; skip caller-cache hits and best-of-N siblings
        if r["source"] == "experiment" and ds.source != "experiment":
            continue  # replays and judge prompts of earlier experiments are not caller traffic
        if ds.source and r["source"] != ds.source:
            continue
        if ds.role and (meta.get("role") or "") != ds.role:
            continue
        if ds.model and r["model_used"] != ds.model:
            continue
        if ds.only_ok and meta.get("parse_ok") is False:
            continue
        out.append(dict(r) | {"meta": meta})
    seen, uniq = set(), []
    for r in out:  # one sample per distinct prompt
        if r["input_json"] not in seen:
            seen.add(r["input_json"])
            uniq.append(r)
    random.Random(ds.seed).shuffle(uniq)
    return uniq[: ds.limit]


# ------------------------------------------------------------------ scorers

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(_FENCE.sub("", text))
    except (json.JSONDecodeError, TypeError):
        return None


def score_json_valid(reference: str, answer: str) -> float | None:
    ref = _json(reference)
    if ref is None:
        return None  # not a structured call
    got = _json(answer)
    if got is None:
        return 0.0
    if isinstance(ref, dict) and isinstance(got, dict):
        return 1.0 if set(ref) <= set(got) else 0.5  # parses but misses top-level keys
    return 1.0


def score_similarity(reference: str, answer: str) -> float:
    a, b = (reference or "")[:6000], (answer or "")[:6000]
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


JUDGE_PROMPT = """You compare two answers to the same request. Judge which answer better fulfils the
request: correctness first, then completeness, then following the requested format. Length is not
a virtue. Reply with JSON only: {{"winner": "A" | "B" | "tie", "reason": "<one sentence>"}}

REQUEST:
{request}

ANSWER A:
{a}

ANSWER B:
{b}"""


def _request_text(messages: list[dict], max_chars: int) -> str:
    parts = []
    for m in messages:
        c = m.get("content")
        c = c if isinstance(c, str) else json.dumps(c)
        parts.append(f"[{m.get('role')}] {c}")
    text = "\n".join(parts)
    return text if len(text) <= max_chars else text[: max_chars // 2] + "\n[...]\n" + text[-max_chars // 2:]


# ------------------------------------------------------------------ runner


class Runner:
    def __init__(self, wrapper: Wrapper, store: Store, spec: Spec, spec_yaml: str, spec_dir: str = "."):
        self.w, self.store, self.spec, self.spec_yaml = wrapper, store, spec, spec_yaml
        self.spent = 0.0
        self.judge_spent = 0.0
        self.rng = random.Random(spec.dataset.seed)
        self.plugins = load_plugins(spec, spec_dir)
        self.ctx = ReplayContext(spec, spec_dir)
        ensure_schema(store)

    async def _call(self, model: str, messages: list[dict], params: dict, tag: dict) -> tuple[Any, float]:
        t = time.perf_counter()
        resp = await self.w.acomplete(messages, dict(params), tag, force_model=model, source="experiment")
        return resp, (time.perf_counter() - t) * 1000

    async def _judge(self, messages, reference: str, answer: str, tag: dict
                     ) -> tuple[float | None, float, str | None]:
        j = self.spec.judge
        if j is None:
            return None, 0.0, "no judge configured"
        if self.judge_spent >= j.max_usd:
            return None, 0.0, "judge budget exhausted"
        cand_first = self.rng.random() < 0.5
        a, b = (answer, reference) if cand_first else (reference, answer)
        prompt = JUDGE_PROMPT.format(request=_request_text(messages, j.max_chars),
                                     a=a[: j.max_chars // 2], b=b[: j.max_chars // 2])
        try:
            resp, _ = await self._call(j.model, [{"role": "user", "content": prompt}],
                                       j.params, tag | {"wrapper_purpose": "judge"})
        except Exception as exc:
            return None, 0.0, f"{type(exc).__name__}: {exc}"[:300]
        cost = (resp._hidden_params.get("wrapper") or {}).get("cost_usd") or 0.0
        self.judge_spent += cost
        verdict = (_json(resp.choices[0].message.content) or {}).get("winner")
        if verdict == "tie":
            return 0.5, cost, None
        if verdict in ("A", "B"):
            cand_won = (verdict == "A") == cand_first
            return (1.0 if cand_won else 0.0), cost, None
        return None, cost, f"unparseable verdict: {(resp.choices[0].message.content or '')[:120]}"

    async def _one(self, sem: asyncio.Semaphore, s: dict, arm: Arm) -> None:
        async with sem:
            if self.spent >= self.spec.budget_usd:
                self.store.put("experiment_results", {
                    "experiment_id": self.spec.id, "arm_id": arm.id, "sample_id": s["id"], "model": arm.model,
                    "ts": time.time(), "ok": 0, "error": BUDGET_ERROR})
                return
            messages, params, tag, reference, original = replay_request(s, arm, self.spec.id, self.ctx)
            row = {"experiment_id": self.spec.id, "arm_id": arm.id, "sample_id": s["id"], "model": arm.model,
                   "ts": time.time()}
            try:
                resp, latency = await self._call(arm.model, messages, params, tag)
            except Exception as exc:
                self.store.put("experiment_results", row | {"ok": 0, "error": f"{type(exc).__name__}: {exc}"[:500]})
                return
            info = resp._hidden_params.get("wrapper") or {}
            cost = info.get("cost_usd") or 0.0
            self.spent += cost
            answer = resp.choices[0].message.content or ""
            u = resp.usage
            row |= {"ok": 1, "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                    "cached_tokens": getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
                    "cost_usd": cost, "latency_ms": latency, "answer": answer}
            if "json_valid" in self.spec.scorers:
                row["json_valid"] = score_json_valid(reference, answer)
            if "similarity" in self.spec.scorers:
                row["similarity"] = score_similarity(reference, answer)
            if self.plugins:
                row["scores_json"] = dumps(self._plugin_scores(s, reference, answer))
            if "judge" in self.spec.scorers:
                row["judge"], row["judge_cost_usd"], row["judge_error"] = await self._judge(
                    original, reference, answer, tag)
            self.store.put("experiment_results", row)

    def _plugin_scores(self, s: dict, reference: str, answer: str) -> dict[str, Any]:
        """name -> score in 0..1 or null; failures never abort the run, they go under ``_errors``."""
        sample_view = {"id": s["id"], "messages": json.loads(s["input_json"]), "reference": reference,
                       "meta": s["meta"], "params": json.loads(s.get("params_json") or "{}")}
        scores: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name, fn in self.plugins.items():
            try:
                v = fn(copy.deepcopy(sample_view), answer)
                if v is None:
                    scores[name] = None
                elif isinstance(v, bool) or not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
                    raise ValueError(f"returned {v!r}; expected a number in 0..1 or None")
                else:
                    scores[name] = float(v)
            except Exception as exc:  # a plugin is foreign code: contain it
                scores[name] = None
                errors[name] = f"{type(exc).__name__}: {exc}"[:200]
        if errors:
            scores["_errors"] = errors
        return scores

    async def run(self) -> list[dict]:
        samples = sample(self.store, self.spec.dataset)
        if not samples:
            raise ValueError("no recorded calls match the dataset filter")
        started = time.time()
        self.store.put("experiments", {"id": self.spec.id, "spec_yaml": self.spec_yaml, "started": started,
                                       "finished": None, "status": "running"}, mode="INSERT OR REPLACE")
        self.store.execute("DELETE FROM experiment_results WHERE experiment_id=?", (self.spec.id,))
        sem = asyncio.Semaphore(self.spec.concurrency)
        await asyncio.gather(*(self._one(sem, s, arm) for s in samples for arm in self.spec.arms))
        self.store.put("experiments", {"id": self.spec.id, "spec_yaml": self.spec_yaml, "started": started,
                                       "finished": time.time(), "status": "done"}, mode="INSERT OR REPLACE")
        self.store.flush()
        return samples


@contextlib.contextmanager
def store_conn(store: Store):
    """A short-lived connection that commits on success and is always closed."""
    from .storage import connect
    conn = connect(store.path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def replay_request(s: dict, arm: Arm, experiment_id: str, ctx: ReplayContext | None = None
                   ) -> tuple[list[dict], dict, dict, str, list[dict]]:
    """The exact request an arm sends for a sample:
    (messages, params, gateway tag, logged answer, the caller's original messages).
    Shared by real runs and --dry so a dry run predicts what a real run does. The original messages are
    what the judge must see: an arm may not win by rewriting the question."""
    original = json.loads(s["input_json"])
    messages = copy.deepcopy(original)
    if ctx is not None and arm.id in ctx.systems:
        messages = _with_system(messages, ctx.systems[arm.id])
    if arm.system_suffix:
        messages = _with_suffix(messages, arm.system_suffix)
    if ctx is not None and arm.id in ctx.transforms:
        out = ctx.transforms[arm.id](copy.deepcopy(messages))
        if not (isinstance(out, list) and out and all(isinstance(m, dict) for m in out)):
            raise ValueError(f"transform of arm {arm.id!r} must return a non-empty list of messages")
        messages = out
    reference = (json.loads(s["output_json"]) or {}).get("content") or ""
    tag = {"experiment": experiment_id, "arm": arm.id, "sample": s["id"], "role": s["meta"].get("role")}
    # replay with the caller's recorded params (gateway traffic records them); arm params win
    params = {k: v for k, v in json.loads(s.get("params_json") or "{}").items()
              if k not in ("metadata", "stream", "stream_options", "timeout")}
    if not isinstance(params.get("response_format"), dict):
        params.pop("response_format", None)  # only a recorded schema can be replayed; `true` is a marker
    params.update(arm.params)
    params = {k: v for k, v in params.items() if v is not None}  # arm param `null` removes the param
    return messages, params, tag, reference, original


def _with_system(messages: list[dict], text: str) -> list[dict]:
    msgs = [dict(m) for m in messages]
    for m in msgs:
        if m.get("role") == "system":
            m["content"] = text
            return msgs
    return [{"role": "system", "content": text}, *msgs]


def _with_suffix(messages: list[dict], suffix: str) -> list[dict]:
    msgs = [dict(m) for m in messages]
    for m in msgs:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            m["content"] = m["content"].rstrip() + "\n\n" + suffix
            return msgs
    return [{"role": "system", "content": suffix}, *msgs]


# ------------------------------------------------------------------ report


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _p(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def bootstrap_ci(xs: list[float], n: int = 2000, seed: int = 0, alpha: float = 0.05
                 ) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean (seeded, so reports are reproducible)."""
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    k = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(k)) / k for _ in range(n))
    return means[int(alpha / 2 * n)], means[min(n - 1, int((1 - alpha / 2) * n))]


def _paired(arm_rows: list[dict], ctl_rows: list[dict], key: str) -> dict[str, Any] | None:
    """Per-sample difference arm - control on samples both answered; mean, CI and n."""
    ctl = {r["sample_id"]: r[key] for r in ctl_rows if r["ok"] and r[key] is not None}
    d = [r[key] - ctl[r["sample_id"]] for r in arm_rows
         if r["ok"] and r[key] is not None and r["sample_id"] in ctl]
    if not d:
        return None
    return {"n": len(d), "mean": sum(d) / len(d), "ci95": bootstrap_ci(d)}


def _plugin_summary(ok_rows: list[dict], name: str) -> dict[str, Any]:
    xs = [r[f"score:{name}"] for r in ok_rows if r[f"score:{name}"] is not None]
    errs: list[str] = []
    for r in ok_rows:
        for msg in (r["_plugin_errors"].get(name),):
            if msg and msg not in errs:
                errs.append(msg)
    return {"n": len(xs), "mean": _mean(xs), "ci95": bootstrap_ci(xs), "errors": errs[:3]}


def report(store: Store, experiment_id: str, samples: list[dict] | None = None) -> dict[str, Any]:
    rows = [dict(r) for r in store.query("SELECT * FROM experiment_results WHERE experiment_id=?", (experiment_id,))]
    arms: dict[str, list[dict]] = {}
    for r in rows:
        arms.setdefault(r["arm_id"], []).append(r)
    out: dict[str, Any] = {"experiment": experiment_id, "arms": {}}
    spec_row = store.query("SELECT spec_yaml FROM experiments WHERE id=?", (experiment_id,))
    order = [a["id"] for a in (yaml.safe_load(spec_row[0]["spec_yaml"]).get("arms", []) if spec_row else [])]
    out["control"] = order[0] if order else None
    spec_d = yaml.safe_load(spec_row[0]["spec_yaml"]) if spec_row else {}
    plugin_names = [scorer_name(r) for r in (spec_d.get("scorers") or []) if r not in BUILTIN_SCORERS]
    out["primary"] = spec_d.get("primary", "judge")
    out["plugins"] = plugin_names
    for r in rows:  # flatten plugin scores so they can be treated like columns
        sc = json.loads(r.get("scores_json") or "{}")
        for n in plugin_names:
            r[f"score:{n}"] = sc.get(n)
        r["_plugin_errors"] = sc.get("_errors") or {}
    if samples is None:
        ids = sorted({r["sample_id"] for r in rows})
        samples = [dict(r) for i in ids for r in store.query(
            "SELECT model_used, cost_usd, prompt_tokens, completion_tokens, latency_ms FROM requests WHERE id=?", (i,))]
    logged_cost = []
    for s in samples:
        c = s.get("cost_usd")
        if c is None:
            try:
                import litellm
                a, b = litellm.cost_per_token(model=s["model_used"], prompt_tokens=s["prompt_tokens"] or 0,
                                              completion_tokens=s["completion_tokens"] or 0)
                c = a + b
            except Exception:
                c = None
        logged_cost.append(c)
    out["logged"] = {"n": len(samples), "mean_cost_usd": _mean(logged_cost),
                     "p50_latency_ms": _p([s.get("latency_ms") for s in samples], .5),
                     "models": sorted({s.get("model_used") for s in samples if s.get("model_used")})}
    for arm, rs in arms.items():
        ok = [r for r in rs if r["ok"]]
        judged = [r["judge"] for r in ok if r["judge"] is not None]
        out["arms"][arm] = {
            "model": rs[0]["model"], "n": len(rs),
            "skipped_budget": sum(1 for r in rs if r["error"] == BUDGET_ERROR),
            "errors": sum(1 for r in rs if not r["ok"] and r["error"] != BUDGET_ERROR),
            "mean_cost_usd": _mean([r["cost_usd"] for r in ok]), "total_cost_usd": sum(r["cost_usd"] or 0 for r in ok),
            "p50_latency_ms": _p([r["latency_ms"] for r in ok], .5), "p90_latency_ms": _p([r["latency_ms"] for r in ok], .9),
            "cached_share": (sum(r["cached_tokens"] or 0 for r in ok) / max(1, sum(r["prompt_tokens"] or 0 for r in ok))),
            "json_valid": _mean([r["json_valid"] for r in ok]), "similarity": _mean([r["similarity"] for r in ok]),
            "judge_score": _mean(judged), "judged": len(judged), "judge_ci95": bootstrap_ci(judged),
            "judge_win_tie_loss": [sum(j == 1.0 for j in judged), sum(j == 0.5 for j in judged),
                                   sum(j == 0.0 for j in judged)],
            "judge_cost_usd": sum(r["judge_cost_usd"] or 0 for r in ok),
            "judge_errors": sorted({r["judge_error"] for r in ok if r.get("judge_error")})[:3],
            "plugins": {n: _plugin_summary(ok, n) for n in plugin_names},
        }
    ctl_rows = arms.get(out["control"] or "", [])
    for arm, rs in arms.items():
        if arm == out["control"] or not ctl_rows:
            continue
        out["arms"][arm]["vs_control"] = {k: _paired(rs, ctl_rows, k)
                                          for k in ("judge", "json_valid", "cost_usd", "latency_ms")}
        for n in plugin_names:
            out["arms"][arm]["vs_control"][n] = _paired(rs, ctl_rows, f"score:{n}")
    return out


def format_report(rep: dict[str, Any]) -> str:
    lg = rep["logged"]
    ctl = rep["arms"].get(rep.get("control") or "", {})
    f = lambda x, d=4: "-" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))  # noqa: E731
    lines = [f"# Experiment {rep['experiment']}",
             f"logged: n={lg['n']} models={','.join(lg['models'])} mean cost ${f(lg['mean_cost_usd'], 5)} "
             f"(list price, no cache discount) p50 {f(lg['p50_latency_ms'], 0)} ms",
             f"control arm: {rep.get('control')} (re-run under the same conditions; costs are compared to it)", "",
             "arm | model | n | err | mean $ | vs control | cached | p50 ms | json ok | similar | judge (win/tie/loss)"]
    for arm, a in sorted(rep["arms"].items(), key=lambda kv: (kv[1]["mean_cost_usd"] or 1e9)):
        rel = (a["mean_cost_usd"] / ctl["mean_cost_usd"]) if (a["mean_cost_usd"] and ctl.get("mean_cost_usd")) else None
        wtl = "/".join(map(str, a["judge_win_tie_loss"]))
        lines.append(" | ".join([arm, a["model"], str(a["n"]), str(a["errors"]), f(a["mean_cost_usd"], 5),
                                 "-" if rel is None else f"{rel:.2f}x", f"{a['cached_share']:.0%}",
                                 f(a["p50_latency_ms"], 0), f(a["json_valid"], 2), f(a["similarity"], 2),
                                 f"{f(a['judge_score'], 2)} ({wtl})"]))
        if a.get("judge_ci95"):
            lo, hi = a["judge_ci95"]
            lines.append(f"    judge 95% CI [{lo:.2f}, {hi:.2f}]")
        for n, pl in (a.get("plugins") or {}).items():
            ci_s = f" 95% CI [{pl['ci95'][0]:.2f}, {pl['ci95'][1]:.2f}]" if pl["ci95"] else ""
            lines.append(f"    {n}: {f(pl['mean'], 2)}{ci_s} over {pl['n']}")
            for e in pl["errors"]:
                lines.append(f"    {n} error: {e}")
        for k, v in (a.get("vs_control") or {}).items():
            if v and (k in ("judge", "json_valid") or k in (rep.get("plugins") or [])):
                ci = v["ci95"]
                sig = "" if not ci else (" (significant)" if ci[0] > 0 or ci[1] < 0 else " (not significant)")
                ci_s = f" CI [{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else ""
                lines.append(f"    paired vs control, {k}: {v['mean']:+.2f}{ci_s} over {v['n']}{sig}")
        if a.get("skipped_budget"):
            lines.append(f"    {a['skipped_budget']} sample(s) skipped: experiment budget exhausted (n above counts them)")
        for e in a.get("judge_errors", []):
            lines.append(f"    judge error: {e}")
    primary = rep.get("primary", "judge")
    lines += ["", "judge: candidate vs. the logged answer, blind and order-randomized; 1 = better, 0.5 = tie, 0 = worse."]
    if primary == "judge":
        lines += ["Promotion bar: judge >= 0.45 with 95% CI low >= 0.35 over >= 20 judged samples, paired judge vs",
                  "control not significantly worse, json ok >= 0.95, cheaper than control."]
    else:
        lines += [f"Promotion bar (primary scorer {primary}): >= 20 scored samples, paired {primary} vs control not",
                  "significantly worse, json ok >= 0.95, cheaper than control."]

    def scored(a: dict[str, Any]) -> int:
        return a["judged"] if primary == "judge" else (a.get("plugins") or {}).get(primary, {}).get("n", 0)

    if any(scored(a) < MIN_DECISION_SAMPLES for a in rep["arms"].values()):
        lines.append(f"exploratory: fewer than {MIN_DECISION_SAMPLES} scored samples; "
                     "only large differences are detectable")
    return "\n".join(lines)


_SAFE_ROLE = re.compile(r"^[\w.-]+$")


def promotion_snippet(rep: dict[str, Any], role: str | None, min_judged: int = 20, min_score: float = 0.45,
                      margin: float = 0.10) -> str:
    lg = rep["logged"]
    ctl = rep["arms"].get(rep.get("control") or "", {})
    best = None
    primary = rep.get("primary", "judge")
    for arm, a in rep["arms"].items():
        if arm == rep.get("control"):
            continue
        if a["errors"]:
            continue
        if primary == "judge":
            if a["judged"] < min_judged or (a["judge_score"] or 0) < min_score:
                continue
            ci = a.get("judge_ci95")
            if ci and ci[0] < min_score - margin:
                continue  # too uncertain: could be clearly worse than the logged answers
            pj = (a.get("vs_control") or {}).get("judge")
            if pj and pj["ci95"] and pj["ci95"][1] < 0:
                continue  # significantly worse than the control arm
        else:  # a plugin scorer decides: enough scored samples and not significantly worse than the control
            pl = (a.get("plugins") or {}).get(primary)
            pp = (a.get("vs_control") or {}).get(primary)
            if not pl or pl["n"] < min_judged or not pp or not pp["ci95"] or pp["ci95"][1] < 0:
                continue
        if ctl.get("mean_cost_usd") and (a["mean_cost_usd"] or 1e9) >= ctl["mean_cost_usd"]:
            continue
        if (a["json_valid"] is not None and a["json_valid"] < 0.95):
            continue
        if best is None or (a["mean_cost_usd"] or 1e9) < (best[1]["mean_cost_usd"] or 1e9):
            best = (arm, a)
    if best is None:
        return "# no arm meets the promotion bar (see report); keep the current route"
    arm, a = best
    if role and not _SAFE_ROLE.match(role):
        return (f"# arm {arm} meets the bar, but role {role!r} has characters that are unsafe in a route "
                "condition; write the `when:` expression by hand")
    when = f"metadata.role == '{role}'" if role else "true"
    from .expr import compile_expr
    compile_expr(when)  # fail here, not when the policy is loaded
    base = ctl.get("mean_cost_usd") or lg["mean_cost_usd"]
    saving = f"{(1 - a['mean_cost_usd'] / base):.0%}" if base else "?"
    if primary == "judge":
        evidence = f"judge {a['judge_score']:.2f} over {a['judged']} samples"
    else:
        pl = a["plugins"][primary]
        evidence = f"{primary} {pl['mean']:.2f} over {pl['n']} samples"
    return (f"# proposal from experiment {rep['experiment']}: arm {arm}, {evidence}, "
            f"cost {saving} lower than the control arm\n"
            f"routes:\n  - id: {rep['experiment']}-{arm}\n    when: \"{when}\"\n    model: {a['model']}\n")


async def run_experiment(wrapper: Wrapper, store: Store, spec_path: str, limit: int | None = None
                         ) -> dict[str, Any]:
    spec, text = load_spec(spec_path, limit)
    runner = Runner(wrapper, store, spec, text, str(Path(spec_path).resolve().parent))
    samples = await runner.run()
    rep = report(store, spec.id, samples)
    rep["promotion"] = promotion_snippet(rep, spec.dataset.role)
    rep["experiment_cost_usd"] = runner.spent + runner.judge_spent
    return rep


def dry_run(wrapper: Wrapper, store: Store, spec_path: str, limit: int | None = None) -> dict[str, Any]:
    """What a run would do, without calling any model or writing anything: per arm, how many samples
    the gateway would refuse or reroute, placeholders, and token and cost estimates. Estimates use list
    prices with no cache discount and the logged answer's length as the output length."""
    import litellm

    from . import size as size_mod
    from .privacy import EgressBlocked, NonTextBlocked

    spec, _ = load_spec(spec_path, limit)
    ctx = ReplayContext(spec, str(Path(spec_path).resolve().parent))
    samples = sample(store, spec.dataset)
    if not samples:
        raise ValueError("no recorded calls match the dataset filter")
    out: dict[str, Any] = {"experiment": spec.id, "samples": len(samples), "arms": {},
                           "judge_max_usd": spec.judge.max_usd if "judge" in spec.scorers and spec.judge else 0.0}
    for arm in spec.arms:
        a = {"model": arm.model, "samples": 0, "would_fail": {}, "rerouted": 0, "placeholders": [],
             "input_tokens": [], "cost": [], "unpriced": 0}
        for smp in samples:
            messages, params, tag, *_ = replay_request(smp, arm, spec.id, ctx)
            try:
                p = wrapper.prepare(messages, dict(params), tag, force_model=arm.model, source="experiment")
            except (size_mod.ContextTooLarge, EgressBlocked, NonTextBlocked) as exc:
                k = type(exc).__name__
                a["would_fail"][k] = a["would_fail"].get(k, 0) + 1
                continue
            a["samples"] += 1
            a["rerouted"] += int(p.model != arm.model)
            a["placeholders"].append(len(p.vault))
            n_in = size_mod.count_tokens(p.model, p.messages, p.tools)
            a["input_tokens"].append(n_in)
            try:
                pin, pout = litellm.cost_per_token(model=p.model, prompt_tokens=n_in,
                                                   completion_tokens=smp.get("completion_tokens") or 0)
                a["cost"].append(pin + pout)
            except Exception:
                a["unpriced"] += 1
        a["mean_placeholders"] = _mean(a.pop("placeholders"))
        a["mean_input_tokens"] = _mean(a.pop("input_tokens"))
        costs = a.pop("cost")
        a["mean_cost_usd"] = _mean(costs)
        a["total_cost_usd"] = sum(costs) if costs else None
        out["arms"][arm.id] = a
    return out


def format_dry_run(d: dict[str, Any]) -> str:
    f = lambda x, n=4: "-" if x is None else f"{x:.{n}f}"  # noqa: E731
    lines = [f"# Dry run {d['experiment']} ({d['samples']} samples) - estimates only, nothing was sent or stored",
             "arm | model | would run | would fail | rerouted | placeholders | input tokens | est. mean $ | est. total $"]
    for arm, a in d["arms"].items():
        fails = ", ".join(f"{k} {v}" for k, v in a["would_fail"].items()) or "0"
        lines.append(" | ".join([arm, a["model"], str(a["samples"]), fails, str(a["rerouted"]),
                                 f(a["mean_placeholders"], 1), f(a["mean_input_tokens"], 0),
                                 f(a["mean_cost_usd"], 5), f(a["total_cost_usd"])]
                                + ([f"({a['unpriced']} unpriced)"] if a["unpriced"] else [])))
    if d["judge_max_usd"]:
        lines.append(f"judge worst case: ${d['judge_max_usd']:.2f} (judge.max_usd)")
    lines.append("estimates: list prices, no cache discount, output length = the logged answer's length.")
    return "\n".join(lines)
