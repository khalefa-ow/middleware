"""Phase-0 analysis of recorded or imported traffic.

Answers the questions that decide what is worth building:
  H1  where the money goes (by role / model), at current LiteLLM prices
  H1b how much prompt is a repeated prefix (what provider prompt caching would save)
  H2  which roles look cheap-model friendly (short structured output, high parse rate)
  H4  what the redaction rules would catch in the prompts (with samples to check for false positives)
  H5  volume per role, to judge whether per-pattern learning can ever get enough samples
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

import litellm

from .privacy import Detector
from .storage import Store


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _price(model: str, pt: int, ct: int) -> float | None:
    try:
        a, b = litellm.cost_per_token(model=model, prompt_tokens=pt, completion_tokens=ct)
        return a + b
    except Exception:
        return None


def _lcp(a: str, b: str) -> int:
    return len(os.path.commonprefix([a, b]))


def _mask(v: str) -> str:
    return v[:2] + "*" * max(0, len(v) - 4) + v[-2:] if len(v) > 4 else "****"


def analyze(store: Store, source: str | None = None, cached_price_ratio: float = 0.1,
            price_model: str | None = None) -> dict[str, Any]:
    """price_model prices every call at one model (useful for public data whose models and
    costs are not yours); cached_price_ratio is the provider's cached-input price / input price."""
    where, args = ("WHERE source=?", [source]) if source else ("", [])
    rows = store.query(f"SELECT * FROM requests {where} ORDER BY session_key, ts", args)
    detector = Detector(["email", "phone", "card", "ssn", "api_key", "ipv4"], skip_code_blocks=True)

    by_role: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    lat: dict[str, list[float]] = defaultdict(list)
    prompt_sizes: list[int] = []
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    pii: dict[str, int] = defaultdict(int)
    pii_samples: dict[str, list[str]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    prefix_total = prefix_shared = 0
    seen_prompts: set[str] = set()
    dup_calls = 0
    prev_by_session: dict[str, list[str]] = defaultdict(list)
    runs: dict[str, bool | None] = {}
    unpriced: set[str] = set()
    caching_saving = 0.0

    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        role = meta.get("role") or r["route_id"] or "?"
        model = r["model_used"] or "?"
        st = by_role[role]
        if meta.get("run_id") is not None:
            runs[f"{r['session_key']}:{meta.get('run_id')}"] = meta.get("run_success")
        if r["error"]:
            st["errors"] += 1
            errors[meta.get("error_type") or r["error"].split(":")[0]] += 1
            continue
        st["calls"] += 1
        if meta.get("cached"):
            st["cache_hits"] += 1
            continue  # answered from the caller's own cache: no spend
        pt, ct = r["prompt_tokens"] or 0, r["completion_tokens"] or 0
        st["prompt_tokens"] += pt
        st["completion_tokens"] += ct
        pm = price_model or model
        cost = r["cost_usd"] if (r["cost_usd"] is not None and not price_model) else _price(pm, pt, ct)
        if cost is None:
            unpriced.add(pm)
            cost = 0.0
        in_price = (_price(pm, 1_000_000, 0) or 0.0) / 1_000_000
        st["cost"] += cost
        by_model[model]["calls"] += 1
        by_model[model]["cost"] += cost
        by_model[model]["tokens"] += pt + ct
        if meta.get("parse_ok") is not None:
            st["parsed"] += 1
            st["parse_ok"] += bool(meta.get("parse_ok"))
        if r["latency_ms"] is not None:
            lat[role].append(r["latency_ms"])
        prompt_sizes.append(pt)

        prompt = r["input_json"] or ""
        # prefix reuse within the same conversation/run (what prompt caching can discount)
        if meta.get("cached_prefix_tokens") is not None:  # exact, from the importer
            cacheable = min(pt, meta["cached_prefix_tokens"])
        else:
            best = max((_lcp(prompt, p) for p in prev_by_session[r["session_key"]][-20:]), default=0)
            cacheable = pt * best / len(prompt) if (pt and prompt) else 0
            prev_by_session[r["session_key"]].append(prompt)
        st["cacheable_prompt_tokens"] += cacheable
        prefix_total += pt
        prefix_shared += cacheable
        caching_saving += cacheable * in_price * (1 - cached_price_ratio)
        # best-of-N siblings share a prompt on purpose; only first candidates count as repeats
        if meta.get("candidate_index") in (0, None):
            if prompt in seen_prompts:
                dup_calls += 1
                st["duplicate_calls"] += 1
            seen_prompts.add(prompt)

        try:
            msgs = json.loads(prompt) if prompt else []
        except json.JSONDecodeError:
            msgs = []
        for m in msgs or []:
            c = m.get("content")
            text = c if isinstance(c, str) else json.dumps(c) if c else ""
            for hit in detector.find(text):
                pii[hit.pattern_id] += 1
                if len(pii_samples[hit.pattern_id]) < 5:
                    ctx = text[max(0, hit.start - 25):hit.start] + "[" + _mask(hit.value) + "]" + text[hit.end:hit.end + 25]
                    pii_samples[hit.pattern_id].append(ctx.replace("\n", " "))

    total_cost = sum(s["cost"] for s in by_role.values())
    roles_out = {}
    for role, s in sorted(by_role.items(), key=lambda kv: -kv[1]["cost"]):
        paid = s["calls"] - s["cache_hits"]
        roles_out[role] = {
            "calls": int(s["calls"]), "paid_calls": int(paid), "cache_hits": int(s["cache_hits"]),
            "errors": int(s["errors"]),
            "prompt_tokens": int(s["prompt_tokens"]), "completion_tokens": int(s["completion_tokens"]),
            "avg_prompt": round(s["prompt_tokens"] / paid) if paid else 0,
            "avg_completion": round(s["completion_tokens"] / paid) if paid else 0,
            "cost_usd": round(s["cost"], 4), "cost_share": round(s["cost"] / total_cost, 3) if total_cost else None,
            "parse_ok_rate": round(s["parse_ok"] / s["parsed"], 3) if s["parsed"] else None,
            "p50_latency_ms": round(_pct(lat[role], .5) or 0), "p90_latency_ms": round(_pct(lat[role], .9) or 0),
            "cacheable_prompt_share": round(s["cacheable_prompt_tokens"] / s["prompt_tokens"], 3)
            if s["prompt_tokens"] else None,
            "duplicate_calls": int(s["duplicate_calls"]),
        }

    cacheable_tokens = sum(s["cacheable_prompt_tokens"] for s in by_role.values())
    finished = [v for v in runs.values() if v is not None]
    return {
        "source": source or "all",
        "requests": len(rows),
        "runs": len(runs), "run_success_rate": round(sum(finished) / len(finished), 3) if finished else None,
        "total_cost_usd": round(total_cost, 4),
        "unpriced_models": sorted(unpriced),
        "by_role": roles_out,
        "by_model": {m: {k: round(v, 4) for k, v in d.items()} for m, d in by_model.items()},
        "errors": dict(errors),
        "prompt_tokens_p50": _pct(prompt_sizes, .5), "prompt_tokens_p90": _pct(prompt_sizes, .9),
        "prompt_tokens_max": max(prompt_sizes) if prompt_sizes else None,
        "prefix_reuse_share": round(prefix_shared / prefix_total, 3) if prefix_total else None,
        "cacheable_prompt_tokens": int(cacheable_tokens),
        "est_prompt_caching_saving_usd": round(caching_saving, 4),
        "price_model": price_model,
        "duplicate_prompt_calls": dup_calls,
        "pii_hits": dict(pii), "pii_samples": dict(pii_samples),
    }


def format_report(rep: dict[str, Any]) -> str:
    out = [f"# Traffic analysis ({rep['source']})", ""]
    out.append(f"requests {rep['requests']} | runs {rep['runs']} | run success {rep['run_success_rate']} | "
               f"spend ${rep['total_cost_usd']:.4f} at current prices"
               + (f" of {rep['price_model']}" if rep.get("price_model") else ""))
    if rep["unpriced_models"]:
        out.append(f"no LiteLLM price for: {', '.join(rep['unpriced_models'])} (counted as $0)")
    out.append("")
    out.append("## By role")
    hdr = ("role", "paid", "hits", "err", "avg_in", "avg_out", "cost$", "share", "parse_ok", "p50ms", "p90ms",
           "prefix%", "dups")
    out.append(" | ".join(hdr))
    for role, s in rep["by_role"].items():
        out.append(" | ".join(str(x) for x in (
            role, s["paid_calls"], s["cache_hits"], s["errors"], s["avg_prompt"], s["avg_completion"],
            f"{s['cost_usd']:.4f}", s["cost_share"], s["parse_ok_rate"], s["p50_latency_ms"], s["p90_latency_ms"],
            s["cacheable_prompt_share"], s["duplicate_calls"])))
    out.append("")
    out.append("## By model")
    for m, d in rep["by_model"].items():
        out.append(f"{m}: calls {int(d['calls'])}, tokens {int(d['tokens'])}, ${d['cost']:.4f}")
    out.append("")
    out.append("## Size and caching")
    out.append(f"prompt tokens p50 {rep['prompt_tokens_p50']}, p90 {rep['prompt_tokens_p90']}, "
               f"max {rep['prompt_tokens_max']}")
    out.append(f"prompt tokens that repeat an earlier call's prefix in the same run: {rep['prefix_reuse_share']}")
    share = rep['est_prompt_caching_saving_usd'] / rep['total_cost_usd'] if rep['total_cost_usd'] else 0
    out.append(f"est. saving from provider prompt caching: ${rep['est_prompt_caching_saving_usd']:.4f} ({share:.0%} of spend)")
    out.append(f"exact duplicate prompts (cacheable answers): {rep['duplicate_prompt_calls']}")
    out.append("")
    out.append("## Errors")
    for k, v in sorted(rep["errors"].items(), key=lambda kv: -kv[1]):
        out.append(f"{k}: {v}")
    out.append("")
    out.append("## Redaction dry run (check samples for false positives)")
    if not rep["pii_hits"]:
        out.append("no hits")
    for pid, n in rep["pii_hits"].items():
        out.append(f"{pid}: {n}")
        for s in rep["pii_samples"].get(pid, []):
            out.append(f"    ...{s}...")
    return "\n".join(out)
