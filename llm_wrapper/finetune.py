"""Fine-tune dataset export: turn recorded calls into a training set for a tuned-model arm.

Two ways to pick the training target for each prompt:
  logged              - the answer the caller actually got (optionally only from successful runs)
  experiment:ID:ARM   - the answer an experiment arm gave where the judge rated it better than
                        the logged answer (distillation from a stronger model)

The export writes OpenAI chat fine-tuning JSONL (``{"messages": [...]}`` per line), split into
train and holdout. The split is stored in ``dataset_splits`` under the export name, so an
experiment can evaluate the tuned model on held-out prompts only (``dataset.holdout_of``).

Training files leave the machine (they are uploaded to a provider), so prompts and answers
are redacted by default with per-sample placeholders; the same value gets the same
placeholder in the prompt and the answer, so the model learns to copy placeholders through.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .experiments import SCHEMA as EXP_SCHEMA, Dataset, sample, store_conn
from .privacy import Detector, RedactionReport, Vault
from .privacy.egress import redact_text
from .storage import Store

DEFAULT_PATTERNS = ["email", "phone", "card", "ssn", "api_key"]


@dataclass
class ExportStats:
    candidates: int = 0
    train: int = 0
    holdout: int = 0
    skipped_no_success: int = 0
    skipped_tools: int = 0
    skipped_non_text: int = 0
    redactions: int = 0
    approx_train_tokens: int = 0
    files: list[str] = field(default_factory=list)


def _targets(store: Store, select: str) -> dict[str, str] | None:
    """For experiment selection: sample_id -> arm answer the judge preferred over the logged answer."""
    if select == "logged":
        return None
    try:
        _, exp_id, arm = select.split(":", 2)
    except ValueError:
        raise ValueError("select must be 'logged' or 'experiment:<id>:<arm>'") from None
    rows = store.query("SELECT sample_id, answer FROM experiment_results WHERE experiment_id=? AND arm_id=? "
                       "AND ok=1 AND judge=1.0", (exp_id, arm))
    if not rows:
        raise ValueError(f"no judged wins for arm {arm!r} in experiment {exp_id!r}")
    return {r["sample_id"]: r["answer"] for r in rows}


def export_finetune(store: Store, name: str, out_dir: str, dataset: Dataset, select: str = "logged",
                    require_success: bool = False, holdout: float = 0.2, redact: bool = True,
                    key: bytes = b"finetune-export", patterns: list[str] | None = None) -> ExportStats:
    stats = ExportStats()
    targets = _targets(store, select)
    ds = dataset.model_copy(update={"limit": 10 ** 9})
    rows = sample(store, ds)
    detector = Detector(patterns or DEFAULT_PATTERNS)
    examples: list[tuple[str, dict]] = []
    for r in rows:
        stats.candidates += 1
        meta = r["meta"]
        if require_success and not (meta.get("task_success") or meta.get("run_success")):
            stats.skipped_no_success += 1
            continue
        out = json.loads(r["output_json"]) or {}
        if out.get("tool_calls"):
            stats.skipped_tools += 1  # tool-call targets need the tools list; not exported yet
            continue
        answer = targets.get(r["id"]) if targets is not None else out.get("content")
        if not answer:
            continue
        prompt = json.loads(r["input_json"])
        if any(not isinstance(m.get("content"), str) or m.get("role") == "tool" or m.get("tool_calls")
               for m in prompt):
            stats.skipped_non_text += 1  # a partial prompt would teach the wrong thing; skip the whole example
            continue
        msgs = [{"role": m.get("role"), "content": m["content"]} for m in prompt]
        msgs.append({"role": "assistant", "content": answer})
        if redact:
            vault, rep = Vault(key, f"{name}:{r['id']}"), RedactionReport()
            for m in msgs:
                m["content"] = redact_text(m["content"], vault, detector, rep)
            stats.redactions += len(rep.matches)
        examples.append((r["id"], {"messages": msgs}))
    # deterministic split by sample id order, shuffled with a fixed seed
    examples.sort(key=lambda e: e[0])
    random.Random(dataset.seed).shuffle(examples)
    n_hold = int(round(len(examples) * holdout)) if len(examples) > 1 else 0
    splits = {"holdout": examples[:n_hold], "train": examples[n_hold:]}
    d = Path(out_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    with store_conn(store) as c:
        c.executescript(EXP_SCHEMA)
        c.execute("DELETE FROM dataset_splits WHERE name=?", (name,))
        for split, exs in splits.items():
            c.executemany("INSERT INTO dataset_splits (name, request_id, split) VALUES (?,?,?)",
                          [(name, rid, split) for rid, _ in exs])
    for split, exs in splits.items():
        path = d / f"{name}.{split}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for _, ex in exs:
                fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
        stats.files.append(str(path))
        setattr(stats, split, len(exs))
    stats.approx_train_tokens = sum(len(json.dumps(ex)) // 4 for _, ex in splits["train"])
    manifest = {"name": name, "select": select, "require_success": require_success, "redacted": redact,
                "dataset": dataset.model_dump(), "stats": stats.__dict__}
    (d / f"{name}.manifest.json").write_text(json.dumps(manifest, indent=2))
    stats.files.append(str(d / f"{name}.manifest.json"))
    return stats


def holdout_spec(name: str, role: str | None, base_model: str, tuned_model: str) -> dict[str, Any]:
    """Experiment spec that compares the tuned model with its base on the held-out prompts."""
    return {"id": f"{name}-holdout", "dataset": {"role": role, "holdout_of": name, "limit": 200},
            "arms": [{"id": "base", "model": base_model}, {"id": "tuned", "model": tuned_model}],
            "scorers": ["json_valid", "similarity", "judge"]}
