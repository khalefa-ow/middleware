"""Command line: wrapper init | validate | import-cppagent | import-chat | analyze | bench | experiment |
export-finetune | delete | purge | health | show."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .policy import PolicyError, load_policy

STARTER = Path(__file__).with_name("starter_policy.yaml")


def _store(args):
    from .storage import Store

    path = args.db or os.environ.get("WRAPPER_DB")
    if not path:
        try:
            path = load_policy(args.policy).policy.storage.path
        except Exception:
            path = "~/.llm_wrapper/wrapper.db"
    return Store(path)


def cmd_init(args) -> int:
    dest = Path(args.path).expanduser()
    if dest.exists() and not args.force:
        print(f"{dest} exists (use --force to overwrite)", file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(STARTER, dest)
    print(f"wrote {dest}")
    return 0


def cmd_validate(args) -> int:
    try:
        lp = load_policy(args.policy, args.overlay or [])
    except PolicyError as exc:
        print(f"INVALID\n{exc}", file=sys.stderr)
        return 1
    p = lp.policy
    from .privacy import Detector

    Detector(p.privacy.patterns, [(c.id, c.type, c.regex) for c in p.privacy.custom_patterns], p.privacy.allow_patterns)
    print(f"OK {lp.source} hash={lp.hash} default={p.models.default} routes={len(p.routes)}")
    return 0


def cmd_import(args) -> int:
    from .importers.cppagent import import_traces

    st = _store(args)
    stats = import_traces(args.path, st, force=args.force)
    st.close()
    print(f"files {stats.files} (unchanged, skipped: {stats.skipped_files}) | records {stats.records} "
          f"| llm errors {stats.errors} | bad lines {len(stats.bad_lines)}")
    for b in stats.bad_lines[:20]:
        print(f"  bad line {b}")
    return 0


def cmd_import_chat(args) -> int:
    from .importers.openai_chat import import_chat_jsonl

    st = _store(args)
    stats = import_chat_jsonl(args.path, st, args.messages_field, source=args.source, id_field=args.id_field,
                              model_field=args.model_field, outcome_field=args.outcome_field,
                              caller_kind=args.kind, redact=not args.no_redact, limit=args.limit)
    st.close()
    print(f"conversations {stats.records} | calls {stats.calls} | bad lines {len(stats.bad_lines)}")
    return 0


def cmd_analyze(args) -> int:
    from .analyze import analyze, format_report

    st = _store(args)
    rep = analyze(st, args.source, cached_price_ratio=args.cached_price_ratio, price_model=args.price_model)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep))
    return 0


def cmd_delete(args) -> int:
    from .privacy import value_hash

    st = _store(args)
    ids = []
    if args.request:
        ids.append(args.request)
    if args.value:
        ids += st.requests_with_value(value_hash(args.value))
        st.execute("DELETE FROM vault WHERE value_hash=?", (value_hash(args.value),))
    n = sum(st.delete_request(i) for i in set(ids))
    print(f"deleted {len(set(ids))} request(s), {n} row(s)")
    return 0


def cmd_purge(args) -> int:
    lp = load_policy(args.policy)
    st = _store(args)
    print(st.purge(lp.policy.retention.requests_days, lp.policy.retention.vault_days))
    return 0


def cmd_health(args) -> int:
    ok = True
    try:
        lp = load_policy(args.policy)
        print(f"policy  ok  {lp.hash}")
    except Exception as exc:
        print(f"policy  FAIL {exc}")
        ok = False
    try:
        st = _store(args)
        n = st.query("SELECT count(*) AS n FROM requests")[0]["n"]
        print(f"db      ok  {st.path} ({n} requests, writer queue {st.queue_depth()})")
    except Exception as exc:
        print(f"db      FAIL {exc}")
        ok = False
    return 0 if ok else 1


def cmd_bench(args) -> int:
    """Overhead benchmark: run every stored full prompt through routing, redaction, leak check
    and size check (no model call) and report the added latency."""
    import time

    from .pipeline import Wrapper
    from .privacy import load_key

    lp = load_policy(args.policy)
    st = _store(args)
    w = Wrapper(lp, None, load_key())
    rows = st.query("SELECT input_json, metadata_json FROM requests WHERE input_json IS NOT NULL")
    prompts = [json.loads(r["input_json"]) for r in rows
               if not json.loads(r["metadata_json"] or "{}").get("delta_input")]
    prompts = [p for p in prompts if isinstance(p, list) and p][: args.limit]
    if not prompts:
        print("no full prompts stored (import cppagent traces or record live traffic first)")
        return 1
    w.prepare(prompts[0], {}, {})  # warm caches (tokenizer, model info, regexes)
    times = []
    for _ in range(args.repeat):
        for p in prompts:
            t = time.perf_counter()
            w.prepare(p, {}, {})
            times.append((time.perf_counter() - t) * 1000)
    times.sort()
    q = lambda f: times[min(len(times) - 1, int(f * len(times)))]  # noqa: E731
    size = sorted(len(json.dumps(p)) for p in prompts)
    print(f"{len(times)} prepares over {len(prompts)} prompts (median {size[len(size)//2]} chars, "
          f"max {size[-1]}): p50 {q(.5):.2f} ms, p95 {q(.95):.2f} ms, p99 {q(.99):.2f} ms, max {times[-1]:.2f} ms")
    return 0


def cmd_experiment(args) -> int:
    import asyncio

    from . import experiments as ex
    from .pipeline import Wrapper
    from .privacy import load_key

    st = _store(args)
    if args.action == "report":
        rep = ex.report(st, args.target)
        print(json.dumps(rep, indent=2) if args.json else ex.format_report(rep))
        return 0
    w = Wrapper(load_policy(args.policy), None if args.dry else st, load_key())
    if args.dry:
        d = ex.dry_run(w, st, args.target)
        print(json.dumps(d, indent=2) if args.json else ex.format_dry_run(d))
        return 0
    rep = asyncio.run(ex.run_experiment(w, st, args.target))
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(ex.format_report(rep))
        print(f"\nexperiment spend ${rep['experiment_cost_usd']:.4f}\n")
        print(rep["promotion"])
    st.close()
    return 0


def cmd_export_finetune(args) -> int:
    from .experiments import Dataset
    from .finetune import export_finetune, holdout_spec

    st = _store(args)
    ds = Dataset(source=args.source, role=args.role, model=args.model, seed=args.seed)
    stats = export_finetune(st, args.name, args.out, ds, select=args.select, require_success=args.require_success,
                            holdout=args.holdout, redact=not args.no_redact)
    print(f"candidates {stats.candidates} | train {stats.train} | holdout {stats.holdout} | "
          f"skipped (no success {stats.skipped_no_success}, tool calls {stats.skipped_tools}, "
          f"non-text {stats.skipped_non_text}) | "
          f"redactions {stats.redactions} | ~{stats.approx_train_tokens} train tokens")
    for f in stats.files:
        print(f"  wrote {f}")
    if stats.train < 10:
        print("warning: fewer than 10 training examples; most providers require at least 10", file=sys.stderr)
    import yaml
    print("\n# after tuning, evaluate on the holdout with an experiment spec like:")
    print(yaml.safe_dump(holdout_spec(args.name, args.role, args.model or "<base model>", "<tuned model id>"),
                         sort_keys=False))
    return 0


def cmd_show(args) -> int:
    st = _store(args)
    rows = st.query("SELECT id, ts, source, route_id, model_used, prompt_tokens, completion_tokens, cost_usd, "
                    "latency_ms, error FROM requests ORDER BY ts DESC LIMIT ?", (args.limit,))
    for r in rows:
        print(dict(r))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wrapper")
    ap.add_argument("--policy", default=os.environ.get("WRAPPER_POLICY", "~/.llm_wrapper/policy.yaml"))
    ap.add_argument("--db", default=None, help="SQLite path (default: policy storage.path)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a commented starter policy")
    s.add_argument("path", nargs="?", default="policy.yaml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("validate", help="validate a policy")
    s.add_argument("policy")
    s.add_argument("--overlay", action="append")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("import-cppagent", help="import cppagent trace files")
    s.add_argument("path", help="a trace .jsonl or a folder searched for .cppagent/traces/*.jsonl")
    s.add_argument("--force", action="store_true", help="re-read files even if unchanged")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("analyze", help="phase-0 traffic report")
    s.add_argument("--source", default=None, help="own_live | own_import")
    s.add_argument("--json", action="store_true")
    s.add_argument("--price-model", help="price every call at this model (e.g. for public data)")
    s.add_argument("--cached-price-ratio", type=float, default=0.1,
                   help="provider's cached-input price as a fraction of the input price")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("delete", help="delete stored data")
    s.add_argument("--request")
    s.add_argument("--value", help="delete every request containing this private value")
    s.set_defaults(fn=cmd_delete)

    s = sub.add_parser("purge", help="apply retention periods")
    s.set_defaults(fn=cmd_purge)

    s = sub.add_parser("health")
    s.set_defaults(fn=cmd_health)

    s = sub.add_parser("bench", help="measure the wrapper's added latency on stored prompts")
    s.add_argument("--limit", type=int, default=1000)
    s.add_argument("--repeat", type=int, default=3)
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("import-chat", help="import OpenAI-style chat logs (JSONL, one conversation per line)")
    s.add_argument("path")
    s.add_argument("--messages-field", default="messages")
    s.add_argument("--id-field")
    s.add_argument("--model-field")
    s.add_argument("--outcome-field", help="truthy field marking task success (e.g. resolved)")
    s.add_argument("--kind", default="chat", help="caller kind recorded as the role (chat | agent)")
    s.add_argument("--source", default="public")
    s.add_argument("--limit", type=int)
    s.add_argument("--no-redact", action="store_true")
    s.set_defaults(fn=cmd_import_chat)

    s = sub.add_parser("experiment", help="replay recorded calls under variants and compare")
    s.add_argument("action", choices=["run", "report"])
    s.add_argument("target", help="spec YAML (run) or experiment id (report)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--dry", action="store_true", help="run: estimate only; no model calls, nothing stored")
    s.set_defaults(fn=cmd_experiment)

    s = sub.add_parser("export-finetune", help="export recorded calls as a fine-tuning dataset (train + holdout)")
    s.add_argument("name", help="export name (also the split name experiments use via dataset.holdout_of)")
    s.add_argument("--out", default=".", help="output folder")
    s.add_argument("--source")
    s.add_argument("--role")
    s.add_argument("--model", help="only calls answered by this model")
    s.add_argument("--select", default="logged", help="logged | experiment:<id>:<arm> (judge-preferred answers)")
    s.add_argument("--require-success", action="store_true", help="only calls from successful tasks/runs")
    s.add_argument("--holdout", type=float, default=0.2)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--no-redact", action="store_true")
    s.set_defaults(fn=cmd_export_finetune)

    s = sub.add_parser("show", help="recent requests")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_show)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
