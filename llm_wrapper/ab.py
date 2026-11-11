"""Run-level A/B harness: compare policies by running the caller's own command.

A recorded call can't be scored by task outcome (the project state at call time isn't recorded), so
for agents the comparison is done per run: start the caller's command N times per arm, each arm under
its own policy, and compare success rate and cost per successful run.

    id: fastlanes-stage2
    command: ["bash", "examples/cppagent_ab/run.sh", "{run_dir}", "{policy}"]
    cwd: .                          # relative to the spec file
    env: {}                         # extra environment for every run
    arms:                           # first arm = control
      - {id: control, policy: policies/base.yaml}
      - {id: luna-planner, policy: policies/base.yaml, overlays: [policies/planner_luna.yaml]}
    runs: 5                         # per arm
    success: {exit_code: 0}         # or {stdout_regex: "^Success: True"}
    timeout_s: 1800
    budget_usd: 2.0
    out_dir: var/ab                 # relative to the spec file

``{run_dir}``, ``{policy}``, ``{arm}`` and ``{i}`` are replaced in every command argument; the command is
never run through a shell. Each run gets WRAPPER_POLICY (the arm's effective policy), WRAPPER_DB (the database
the harness reads) and WRAPPER_RUN_TAG (``<id>:<arm>:<i>``; the gateway stamps it on every row it records, which
is how a run's calls and cost are found). The policy files written for the arms keep ``${ENV:...}`` references
unresolved, so no secret is written to disk. Arms are interleaved (run 0 of every arm, then run 1, ...) so drift
over time hits all arms equally. A spec runs your command: only run specs you trust.
"""

from __future__ import annotations

import os
import random
import re
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from .experiments import _Strict, store_conn
from .policy import load_policy
from .storage import Store

AB_SCHEMA = """
CREATE TABLE IF NOT EXISTS ab_experiments (id TEXT PRIMARY KEY, spec_yaml TEXT, started REAL, finished REAL);
CREATE TABLE IF NOT EXISTS ab_runs (
  experiment_id TEXT, arm_id TEXT, run_index INTEGER, tag TEXT, success INTEGER, exit_code INTEGER,
  timed_out INTEGER, duration_s REAL, calls INTEGER, errors INTEGER, prompt_tokens INTEGER,
  completion_tokens INTEGER, cost_usd REAL, started REAL, skipped INTEGER DEFAULT 0,
  PRIMARY KEY (experiment_id, arm_id, run_index)
);
"""


class ABArm(_Strict):
    id: str = Field(pattern=r"^[\w.-]+$")
    policy: str
    overlays: list[str] = []
    env: dict[str, str] = {}


class Success(_Strict):
    exit_code: int | None = None
    stdout_regex: str | None = None

    @model_validator(mode="after")
    def _one(self) -> "Success":
        if (self.exit_code is None) == (self.stdout_regex is None):
            raise ValueError("success needs exactly one of exit_code or stdout_regex")
        if self.stdout_regex is not None:
            re.compile(self.stdout_regex)
        return self


class ABSpec(_Strict):
    id: str = Field(pattern=r"^[\w.-]+$")
    command: list[str] = Field(min_length=1)
    cwd: str = "."
    env: dict[str, str] = {}
    arms: list[ABArm] = Field(min_length=1)
    runs: int = Field(5, ge=1)
    success: Success = Success(exit_code=0)
    timeout_s: float = Field(1800, gt=0)
    budget_usd: float = Field(2.0, ge=0)
    out_dir: str = "var/ab"

    @model_validator(mode="after")
    def _unique_arms(self) -> "ABSpec":
        ids = [a.id for a in self.arms]
        if len(set(ids)) != len(ids):
            raise ValueError("arm ids must be unique")
        return self


def load_ab_spec(path: str) -> tuple[ABSpec, str]:
    text = Path(path).read_text()
    return ABSpec.model_validate(yaml.safe_load(text)), text


def _fill(arg: str, **values: str) -> str:
    for k, v in values.items():
        arg = arg.replace("{" + k + "}", v)
    return arg


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the command may start children (a shell script, then python)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _execute(argv: list[str], cwd: str, env: dict[str, str], run_dir: Path, timeout_s: float
             ) -> tuple[int | None, bool, float]:
    """Run one command; stdout and stderr go to files in the run folder. (exit code, timed out, seconds)"""
    t0 = time.time()
    with open(run_dir / "stdout.log", "wb") as out, open(run_dir / "stderr.log", "wb") as err:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=timeout_s)
            return code, False, time.time() - t0
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
            return None, True, time.time() - t0


def _is_success(spec: ABSpec, code: int | None, timed_out: bool, run_dir: Path) -> bool:
    if timed_out:
        return False
    if spec.success.exit_code is not None:
        return code == spec.success.exit_code
    text = (run_dir / "stdout.log").read_text(errors="replace")
    return re.search(spec.success.stdout_regex, text, re.M) is not None


def run_ab(store: Store, spec_path: str, log=print) -> dict[str, Any]:
    spec, text = load_ab_spec(spec_path)
    base = Path(spec_path).resolve().parent
    out_root = (base / spec.out_dir / spec.id).resolve()
    cwd = str((base / spec.cwd).resolve())
    db_path = str(Path(store.path).expanduser().resolve())
    # load every policy first: a bad policy fails before any run starts
    policy_files: dict[str, Path] = {}
    out_root.mkdir(parents=True, exist_ok=True)
    for arm in spec.arms:
        lp = load_policy(base / arm.policy, [base / o for o in arm.overlays])
        f = out_root / f"{arm.id}.policy.yaml"
        f.write_text(lp.effective_yaml)  # secrets stay as ${ENV:...} references
        policy_files[arm.id] = f
    with store_conn(store) as c:
        c.executescript(AB_SCHEMA)
        c.execute("DELETE FROM ab_runs WHERE experiment_id=?", (spec.id,))
        c.execute("INSERT OR REPLACE INTO ab_experiments (id, spec_yaml, started, finished) VALUES (?,?,?,NULL)",
                  (spec.id, text, time.time()))
    spent = 0.0
    for i in range(spec.runs):
        for arm in spec.arms:
            tag = f"{spec.id}:{arm.id}:{i}"
            if spent >= spec.budget_usd:
                _put_run(store, spec.id, arm.id, i, tag, skipped=True)
                log(f"[{tag}] skipped: budget ${spec.budget_usd:.2f} reached (spent ${spent:.4f})")
                continue
            run_dir = out_root / arm.id / str(i)
            run_dir.mkdir(parents=True, exist_ok=True)
            fill = dict(run_dir=str(run_dir), policy=str(policy_files[arm.id]), arm=arm.id, i=str(i))
            argv = [_fill(a, **fill) for a in spec.command]
            env = {**os.environ, **spec.env, **arm.env, "WRAPPER_POLICY": str(policy_files[arm.id]),
                   "WRAPPER_DB": db_path, "WRAPPER_RUN_TAG": tag}
            started = time.time()
            code, timed_out, secs = _execute(argv, cwd, env, run_dir, spec.timeout_s)
            ok = _is_success(spec, code, timed_out, run_dir)
            usage = _usage(store, tag)
            spent += usage["cost_usd"]
            _put_run(store, spec.id, arm.id, i, tag, success=ok, exit_code=code, timed_out=timed_out,
                     duration_s=secs, started=started, **usage)
            log(f"[{tag}] {'success' if ok else 'FAILED'}{' (timed out)' if timed_out else ''} "
                f"exit={code} {secs:.0f}s calls={usage['calls']} cost=${usage['cost_usd']:.4f}")
    with store_conn(store) as c:
        c.execute("UPDATE ab_experiments SET finished=? WHERE id=?", (time.time(), spec.id))
    return ab_report(store, spec.id)


def _usage(store: Store, tag: str) -> dict[str, Any]:
    r = store.query("SELECT count(*) AS calls, coalesce(sum(error IS NOT NULL), 0) AS errors, "
                    "coalesce(sum(prompt_tokens), 0) AS pt, coalesce(sum(completion_tokens), 0) AS ct, "
                    "coalesce(sum(cost_usd), 0) AS cost FROM requests WHERE run_tag=?", (tag,))[0]
    return {"calls": r["calls"], "errors": r["errors"], "prompt_tokens": r["pt"], "completion_tokens": r["ct"],
            "cost_usd": float(r["cost"])}


def _put_run(store: Store, exp: str, arm: str, i: int, tag: str, skipped: bool = False, success: bool = False,
             exit_code: int | None = None, timed_out: bool = False, duration_s: float | None = None,
             started: float | None = None, calls: int = 0, errors: int = 0, prompt_tokens: int = 0,
             completion_tokens: int = 0, cost_usd: float = 0.0) -> None:
    with store_conn(store) as c:
        c.execute("INSERT OR REPLACE INTO ab_runs (experiment_id, arm_id, run_index, tag, success, exit_code, "
                  "timed_out, duration_s, calls, errors, prompt_tokens, completion_tokens, cost_usd, started, "
                  "skipped) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (exp, arm, i, tag, int(success), exit_code, int(timed_out), duration_s, calls, errors,
                   prompt_tokens, completion_tokens, cost_usd, started or time.time(), int(skipped)))


# ------------------------------------------------------------------ report


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a proportion."""
    if n == 0:
        return None
    p = successes / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap_diff(a: list[float], b: list[float], n: int = 2000, seed: int = 0, alpha: float = 0.05
                   ) -> tuple[float, float] | None:
    """Percentile bootstrap CI of mean(a) - mean(b), resampling each group independently (seeded)."""
    if not a or not b:
        return None
    rng = random.Random(seed)
    diffs = sorted(sum(rng.choices(a, k=len(a))) / len(a) - sum(rng.choices(b, k=len(b))) / len(b)
                   for _ in range(n))
    return diffs[int(alpha / 2 * n)], diffs[min(n - 1, int((1 - alpha / 2) * n))]


def ab_report(store: Store, experiment_id: str) -> dict[str, Any]:
    try:
        spec_row = store.query("SELECT spec_yaml FROM ab_experiments WHERE id=?", (experiment_id,))
    except sqlite3.OperationalError:  # a database that has never run an A/B has no such table
        spec_row = []
    if not spec_row:
        raise ValueError(f"no A/B experiment named {experiment_id!r}")
    spec = ABSpec.model_validate(yaml.safe_load(spec_row[0]["spec_yaml"]))
    rows = [dict(r) for r in store.query("SELECT * FROM ab_runs WHERE experiment_id=? ORDER BY run_index",
                                         (experiment_id,))]
    out: dict[str, Any] = {"experiment": experiment_id, "control": spec.arms[0].id, "arms": {}}
    outcomes: dict[str, list[float]] = {}
    for arm in spec.arms:
        mine = [r for r in rows if r["arm_id"] == arm.id]
        done = [r for r in mine if not r["skipped"]]
        wins = sum(r["success"] for r in done)
        cost = sum(r["cost_usd"] or 0 for r in done)
        outcomes[arm.id] = [float(r["success"]) for r in done]
        out["arms"][arm.id] = {
            "runs": len(done), "skipped": len(mine) - len(done), "successes": wins,
            "success_rate": wins / len(done) if done else None, "success_ci95": wilson(wins, len(done)),
            "mean_cost_usd": cost / len(done) if done else None,
            "cost_per_success_usd": cost / wins if wins else None,
            "mean_calls": sum(r["calls"] or 0 for r in done) / len(done) if done else None,
            "mean_duration_s": sum(r["duration_s"] or 0 for r in done) / len(done) if done else None,
            "timeouts": sum(r["timed_out"] for r in done),
        }
    ctl = outcomes[out["control"]]
    for arm in spec.arms[1:]:
        mine = outcomes[arm.id]
        if not mine or not ctl:
            continue
        ci = bootstrap_diff(mine, ctl)
        out["arms"][arm.id]["vs_control"] = {
            "success_rate_diff": sum(mine) / len(mine) - sum(ctl) / len(ctl), "ci95": ci,
            "significant": bool(ci and (ci[0] > 0 or ci[1] < 0)),
        }
    return out


def format_ab_report(rep: dict[str, Any]) -> str:
    f = lambda x, d=4: "-" if x is None else f"{x:.{d}f}"  # noqa: E731
    lines = [f"# A/B experiment {rep['experiment']} (control: {rep['control']})", "",
             "arm | runs | success | rate (95% Wilson) | $/run | $/success | calls/run | s/run"]
    for arm, a in rep["arms"].items():
        ci = a["success_ci95"]
        rate = "-" if a["success_rate"] is None else f"{a['success_rate']:.0%} [{ci[0]:.0%}, {ci[1]:.0%}]"
        extra = f" ({a['skipped']} skipped: budget)" if a["skipped"] else ""
        lines.append(" | ".join([arm, f"{a['runs']}{extra}", f"{a['successes']}/{a['runs']}", rate,
                                 f(a["mean_cost_usd"]), f(a["cost_per_success_usd"]), f(a["mean_calls"], 1),
                                 f(a["mean_duration_s"], 0)]))
        v = a.get("vs_control")
        if v:
            lo, hi = v["ci95"]
            verdict = "CI excludes 0" if v["significant"] else "CI includes 0: not significant"
            lines.append(f"    vs control: success rate {v['success_rate_diff']:+.0%} "
                         f"(bootstrap 95% CI [{lo:+.0%}, {hi:+.0%}]; {verdict})")
    lines += ["", "With few runs per arm only large differences are detectable; treat small gaps as noise."]
    return "\n".join(lines)
