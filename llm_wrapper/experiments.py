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
"""

from __future__ import annotations

import asyncio
import difflib
import json
import random
import re
import time
import uuid
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .pipeline import Wrapper
from .storage import Store, dumps


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    system_suffix: str | None = None


class Judge(_Strict):
    model: str
    params: dict[str, Any] = {}
    """Judge call params; empty by default because reasoning models reject temperature=0."""
    max_usd: float = 0.5
    max_chars: int = 12000


class Spec(_Strict):
    id: str
    dataset: Dataset = Dataset()
    arms: list[Arm] = Field(min_length=1)
    scorers: list[Literal["json_valid", "similarity", "judge"]] = ["json_valid", "similarity"]
    judge: Judge | None = None
    budget_usd: float = 1.0
    concurrency: int = Field(4, ge=1, le=32)


SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, spec_yaml TEXT, started REAL, finished REAL, status TEXT);
CREATE TABLE IF NOT EXISTS experiment_results (
  experiment_id TEXT, arm_id TEXT, sample_id TEXT, model TEXT, ok INTEGER, error TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER, cost_usd REAL, latency_ms REAL,
  answer TEXT, json_valid REAL, similarity REAL, judge REAL, judge_cost_usd REAL, judge_error TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS exp_res ON experiment_results(experiment_id, arm_id);
CREATE TABLE IF NOT EXISTS dataset_splits (name TEXT, request_id TEXT, split TEXT, PRIMARY KEY (name, request_id));
"""


def load_spec(path: str) -> tuple[Spec, str]:
    text = open(path).read()
    return Spec.model_validate(yaml.safe_load(text)), text


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
    def __init__(self, wrapper: Wrapper, store: Store, spec: Spec, spec_yaml: str):
        self.w, self.store, self.spec, self.spec_yaml = wrapper, store, spec, spec_yaml
        self.spent = 0.0
        self.judge_spent = 0.0
        self.rng = random.Random(spec.dataset.seed)
        with store_conn(store) as c:
            c.executescript(SCHEMA)
            if "judge_error" not in {r[1] for r in c.execute("PRAGMA table_info(experiment_results)")}:
                c.execute("ALTER TABLE experiment_results ADD COLUMN judge_error TEXT")

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
                return
            messages = json.loads(s["input_json"])
            if arm.system_suffix:
                messages = _with_suffix(messages, arm.system_suffix)
            reference = (json.loads(s["output_json"]) or {}).get("content") or ""
            tag = {"experiment": self.spec.id, "arm": arm.id, "sample": s["id"], "role": s["meta"].get("role")}
            # replay with the caller's recorded params (gateway traffic records them); arm params win
            params = {k: v for k, v in json.loads(s.get("params_json") or "{}").items()
                      if k not in ("response_format", "metadata", "stream", "stream_options", "timeout")}
            params.update(arm.params)
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
            if "judge" in self.spec.scorers:
                row["judge"], row["judge_cost_usd"], row["judge_error"] = await self._judge(
                    messages, reference, answer, tag)
            self.store.put("experiment_results", row)

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


def store_conn(store: Store):
    from .storage import connect
    return connect(store.path)


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


def report(store: Store, experiment_id: str, samples: list[dict] | None = None) -> dict[str, Any]:
    rows = [dict(r) for r in store.query("SELECT * FROM experiment_results WHERE experiment_id=?", (experiment_id,))]
    arms: dict[str, list[dict]] = {}
    for r in rows:
        arms.setdefault(r["arm_id"], []).append(r)
    out: dict[str, Any] = {"experiment": experiment_id, "arms": {}}
    spec_row = store.query("SELECT spec_yaml FROM experiments WHERE id=?", (experiment_id,))
    order = [a["id"] for a in (yaml.safe_load(spec_row[0]["spec_yaml"]).get("arms", []) if spec_row else [])]
    out["control"] = order[0] if order else None
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
            "model": rs[0]["model"], "n": len(rs), "errors": len(rs) - len(ok),
            "mean_cost_usd": _mean([r["cost_usd"] for r in ok]), "total_cost_usd": sum(r["cost_usd"] or 0 for r in ok),
            "p50_latency_ms": _p([r["latency_ms"] for r in ok], .5), "p90_latency_ms": _p([r["latency_ms"] for r in ok], .9),
            "cached_share": (sum(r["cached_tokens"] or 0 for r in ok) / max(1, sum(r["prompt_tokens"] or 0 for r in ok))),
            "json_valid": _mean([r["json_valid"] for r in ok]), "similarity": _mean([r["similarity"] for r in ok]),
            "judge_score": _mean(judged), "judged": len(judged), "judge_ci95": bootstrap_ci(judged),
            "judge_win_tie_loss": [sum(j == 1.0 for j in judged), sum(j == 0.5 for j in judged),
                                   sum(j == 0.0 for j in judged)],
            "judge_cost_usd": sum(r["judge_cost_usd"] or 0 for r in ok),
            "judge_errors": sorted({r["judge_error"] for r in ok if r.get("judge_error")})[:3],
        }
    ctl_rows = arms.get(out["control"] or "", [])
    for arm, rs in arms.items():
        if arm == out["control"] or not ctl_rows:
            continue
        out["arms"][arm]["vs_control"] = {k: _paired(rs, ctl_rows, k)
                                          for k in ("judge", "json_valid", "cost_usd", "latency_ms")}
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
        for k, v in (a.get("vs_control") or {}).items():
            if v and k in ("judge", "json_valid"):
                ci = v["ci95"]
                sig = "" if not ci else (" (significant)" if ci[0] > 0 or ci[1] < 0 else " (not significant)")
                ci_s = f" CI [{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else ""
                lines.append(f"    paired vs control, {k}: {v['mean']:+.2f}{ci_s} over {v['n']}{sig}")
        for e in a.get("judge_errors", []):
            lines.append(f"    judge error: {e}")
    lines += ["", "judge: candidate vs. the logged answer, blind and order-randomized; 1 = better, 0.5 = tie, 0 = worse.",
              "Promotion bar: judge >= 0.45 with 95% CI low >= 0.35 over >= 20 judged samples, paired judge vs",
              "control not significantly worse, json ok >= 0.95, cheaper than control."]
    return "\n".join(lines)


def promotion_snippet(rep: dict[str, Any], role: str | None, min_judged: int = 20, min_score: float = 0.45,
                      margin: float = 0.10) -> str:
    lg = rep["logged"]
    ctl = rep["arms"].get(rep.get("control") or "", {})
    best = None
    for arm, a in rep["arms"].items():
        if arm == rep.get("control"):
            continue
        if a["errors"] or a["judged"] < min_judged or (a["judge_score"] or 0) < min_score:
            continue
        ci = a.get("judge_ci95")
        if ci and ci[0] < min_score - margin:
            continue  # too uncertain: could be clearly worse than the logged answers
        pj = (a.get("vs_control") or {}).get("judge")
        if pj and pj["ci95"] and pj["ci95"][1] < 0:
            continue  # significantly worse than the control arm
        if ctl.get("mean_cost_usd") and (a["mean_cost_usd"] or 1e9) >= ctl["mean_cost_usd"]:
            continue
        if (a["json_valid"] is not None and a["json_valid"] < 0.95):
            continue
        if best is None or (a["mean_cost_usd"] or 1e9) < (best[1]["mean_cost_usd"] or 1e9):
            best = (arm, a)
    if best is None:
        return "# no arm meets the promotion bar (see report); keep the current route"
    arm, a = best
    when = f"metadata.role == '{role}'" if role else "true"
    base = ctl.get("mean_cost_usd") or lg["mean_cost_usd"]
    saving = f"{(1 - a['mean_cost_usd'] / base):.0%}" if base else "?"
    return (f"# proposal from experiment {rep['experiment']}: arm {arm}, judge {a['judge_score']:.2f} over "
            f"{a['judged']} samples, cost {saving} lower than the control arm\n"
            f"routes:\n  - id: {rep['experiment']}-{arm}\n    when: \"{when}\"\n    model: {a['model']}\n")


async def run_experiment(wrapper: Wrapper, store: Store, spec_path: str) -> dict[str, Any]:
    spec, text = load_spec(spec_path)
    runner = Runner(wrapper, store, spec, text)
    samples = await runner.run()
    rep = report(store, spec.id, samples)
    rep["promotion"] = promotion_snippet(rep, spec.dataset.role)
    rep["experiment_cost_usd"] = runner.spent + runner.judge_spent
    return rep
