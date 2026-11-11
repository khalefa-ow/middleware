# Handoff: next work on the LLM wrapper

This file is the working brief for the next engineer on this project (written for Claude Sonnet).
It is self-contained: you don't need the earlier conversation. Read sections 1–4 fully before
touching code, then do the tasks in section 5 in order. If your context is ever summarized,
re-read this file and the progress log at the bottom before continuing.

## 1. What this project is

`llm_wrapper` is a platform to optimize and tune LLM usage. It has one loop:

1. **Record.** A LiteLLM custom provider (`model="wrapper/auto"`) sits between a caller and the
   real model. It routes by policy, redacts private data before the call, restores it in the
   answer, checks the request fits the model, and records every call in SQLite.
2. **Experiment.** Recorded calls are replayed under variants ("arms": another model, other
   params, another prompt). Every replay goes through the same gateway pipeline.
3. **Compare.** Arms are scored on cost, latency, valid JSON, similarity and a blind LLM judge,
   against a control arm, with confidence intervals.
4. **Promote.** If an arm clears the bar, the tool prints a policy route for a person to approve.

The first real caller is `cppagent` (a C++ coding agent in a sibling folder). Its recorded
traces for the "fastlanes" task are the main test data.

`PLAN.md` is the full plan (goal, evidence, gates, milestones). `README.md` is the user guide.
Both are current as of 2026-11-11. This file turns the plan's next milestones into tasks.

### State on 2026-11-11 (all verified)

| Part | State |
|---|---|
| Gateway (route, redact, restore, size, stream, record) | Built and tested; ran live against DeepSeek |
| Importers | cppagent traces; OpenAI-style chat JSONL |
| Analysis (`wrapper analyze`, `wrapper bench`) | Built; overhead p95 5.2 ms |
| Prompt caching through the gateway | Verified live; cached tokens recorded and priced |
| Experiments (`wrapper experiment run/report`) | Built, with bootstrap 95% CIs and a paired comparison with the control |
| Fine-tune export (`wrapper export-finetune`) | Built, with a stored train/holdout split |
| Tests | 65 pass, offline, in about 6 s |
| Git | The folder is **not** a git repository |
| Money spent on live calls so far | About $0.60 |

What the first experiments found (20 samples per role, on fastlanes): `openai/gpt-6-luna` is
not a good replacement for `deepseek/deepseek-chat` for the planner or repair roles. Nothing
was promoted. Fastlanes has only 13–15 successful examples per role, too few to fine-tune on.

## 2. Setup and sanity checks

Work in `/mnt/data/agent-workspace/projects/agentmiddlelayer`. The virtualenv is `.venv`
(Python 3.12, managed by `uv`; LiteLLM 1.108.4).

Run these first. If any result differs, stop and report it before changing anything.

```bash
.venv/bin/pytest -q                      # expect: 65 passed
.venv/bin/wrapper --db var/fastlanes.db experiment report fastlanes-planner
                                         # expect a table where arm "luna" costs 0.44x the control
.venv/bin/wrapper --db var/fastlanes.db analyze --source own_import | head -20
                                         # expect 533 calls; repair is about 67% of spend
```

`var/fastlanes.db` holds the 533 imported fastlanes calls plus the results of the first two
experiments. Treat it as read-only reference data: copy it before any run that writes
(`cp var/fastlanes.db var/work.db`). It can be rebuilt (without the experiment results) with
`wrapper --db NEW.db import-cppagent /mnt/data/agent-workspace/projects/cppagent-usecases/fastlanes`.

## 3. Rules

These apply to every task. They are not suggestions.

**Money and network**
- Everything in section 5 is offline. Don't make any call to a real model, and don't download
  anything, while doing section 5. Tests fake the model (see section 4).
- Section 6 lists work that costs money or changes things outside this folder. Don't start any
  of it. When you reach it, stop and ask the user, quoting the estimated cost.

**Secrets**
- API keys live in `/mnt/data/agent-workspace/projects/cppagent/.env`. Never print, copy, log
  or store their values. Printing variable names is fine.
- Never write a real key, or a real person's data, into a test, example or doc.

**Other folders**
- `/mnt/data/agent-workspace/projects/cppagent` and `.../cppagent-usecases` belong to the user
  and have uncommitted changes. Read them freely. Never modify, format or delete anything there.

**Code**
- Don't add dependencies. `pyproject.toml` lists `litellm`, `pydantic`, `pyyaml`; the standard
  library covers everything in section 5.
- Keep the code style of the files you edit: `from __future__ import annotations`, type hints,
  lines up to 120 characters, short docstrings, comments only where the reason isn't obvious.
- Pydantic models for config use `extra="forbid"` (see `_Strict` in `policy.py` and
  `experiments.py`). New config fields need a default, so existing files keep loading.
- Database changes must work on existing database files: add columns with the
  `MIGRATIONS` map in `storage.py`, or the "ALTER if missing" pattern in `experiments.py`.
  Never drop or rename a column.
- Don't refactor or rename things a task doesn't mention.

**Tests**
- Every task adds tests. The full suite must pass before you start the next task.
- Never delete or weaken an existing test to get a pass. Only change an existing test where a
  task below says to.
- If the same fix fails twice, stop, write down what you tried and what happened, and ask.

**Docs**
- When a task changes behavior a user can see, update `README.md` in the same task.
- When a task finishes a milestone bullet in `PLAN.md` §7, mark it there and update the status
  table in §2. Keep the test count in `README.md` and `PLAN.md` correct.
- After each task, append one line to the progress log at the bottom of this file.

**Git**
- The folder isn't a git repository. Don't run `git init` or commit unless the user asks.

## 4. Code map

| File | What it holds | Things to know |
|---|---|---|
| `llm_wrapper/__init__.py` | `register()`, `build_wrapper()`, `proxy_handler` | Reads `WRAPPER_POLICY` and `WRAPPER_DB` from the environment |
| `provider.py` | `WrapperProvider(CustomLLM)` | Sync entry points run on one background event loop. Client errors become `CustomLLMError(400)` |
| `pipeline.py` | `Wrapper`: `prepare`, `acomplete`, `astream`, `_record` | The core. `prepare` does route → redact → leak check → size check and makes no model call. `self.acompletion` is the seam tests replace |
| `policy.py` | Policy schema and loading | Layers merge; `${ENV:}` / `${FILE:}` secrets; `load_policy()` returns `LoadedPolicy` |
| `expr.py` | Safe expression language for route conditions | `compile_expr`, `evaluate` |
| `privacy/patterns.py` | Detectors | Tuned on real traces; each regex has regression tests. Don't loosen them |
| `privacy/vault.py` | Placeholders (`<EMAIL_1a2b3c4d>`), `load_key()` | `WRAPPER_PRIVACY_KEY` (hex) overrides the key file in `~/.llm_wrapper/key` |
| `privacy/egress.py` | `redact_payload`, `leak_check`, `add_hint`, `restore_text`, `StreamRestorer` | |
| `size.py` | Token budget check | On the fast path `SizeCheck.tokens` is an estimate (bytes / 4). Use `count_tokens()` when you need the exact number |
| `storage.py` | `Store`: background writer (`put`), `query`, `execute` | Call `store.flush()` before reading rows you just wrote |
| `experiments.py` | Spec, `sample()`, `Runner`, scorers, judge, `report()`, `promotion_snippet()` | The first arm in a spec is the control |
| `finetune.py` | `export_finetune()` | Uses `experiments.sample()` |
| `cli.py` | The `wrapper` command | Global flags `--policy` and `--db` go before the subcommand |
| `tests/conftest.py` | `FakeModel`, fixtures `store` and `make_wrapper`, `KEY` | See below |

How tests fake the model:

```python
async def test_example(make_wrapper, store):
    fake = FakeModel(replies=["hello"])
    w = make_wrapper(fake, privacy={"trusted": []})      # keyword args override policy sections
    resp = await w.acomplete([{"role": "user", "content": "hi"}], {"temperature": 0}, {"role": "planner"})
    assert fake.calls[0]["model"] == "openai/gpt-4o-mini"   # what was sent to the "model"
    store.flush()
    row = store.query("SELECT * FROM requests")[0]
```

`fake.calls` holds the keyword arguments of every outbound call, and `fake.sent` is their
messages and tools as one JSON string (handy for "this value never left" assertions). Test
functions are plain `async def`; `asyncio_mode = "auto"` is set.

Facts about the environment that have caused bugs before:
- LiteLLM doesn't pass `user` to custom providers. Caller identity comes from `metadata`.
- LiteLLM turns a pydantic class given as `response_format` into a `json_schema` dict before
  the provider sees it.
- `openai/gpt-5.3-codex` rejects `temperature=0`. That is why judge params default to `{}`.
- cppagent's traces don't record `temperature` or `response_format`, so replays of imported
  calls are not faithful. Only compare arms with the control arm, never with the logged answer.
- Models the project's keys can reach: `deepseek/deepseek-chat`, `deepseek/deepseek-reasoner`,
  `openai/gpt-6-luna`, `openai/gpt-5.3-codex`. The `gpt-4.1` family is not available. (Model
  names in tests are only strings used for price lookup.)

## 5. Tasks

Do them in order. Each task lists where to work, the exact behavior, the tests to add and
when it is done. Phases 1 and 2 matter most; if time runs out, finish a phase cleanly rather
than starting the next.

### Phase 1: correctness fixes

#### T1. `record_inputs: false` must also keep real values out of the vault table
- **Problem.** With `storage.record_inputs: false`, `Wrapper._record` (`pipeline.py`) leaves
  `input_json` and `output_json` empty but still writes every redacted value, in clear text,
  to the `vault` table.
- **Change.** In `_record`, when `record_inputs` is false, write `value: None` in the `vault`
  row. Keep `placeholder`, `scope_key`, `value_hash`, `type`, `origin`, and keep writing
  `vault_refs`. Nothing reads `vault.value` back (restore uses the in-memory vault), so no
  other code changes.
- **Tests** (`tests/test_pipeline.py`). With `make_wrapper(fake, storage={"record_inputs": False})`
  and a message containing `jane@acme.com`:
  - no column of any row in `requests`, `vault`, `redactions` or `spans` contains `jane@acme.com`
  - `store.requests_with_value(value_hash("jane@acme.com"))` still returns the request id
  - the reply is still restored for the caller
- **Done when** those pass and the starter policy's comment on `record_inputs` says the vault
  is covered.

#### T2. A streaming request that fails before the model call must be recorded
- **Problem.** In `Wrapper.astream`, `self.prepare(...)` runs outside the `try`. If it raises
  (`ContextTooLarge`, `EgressBlocked`, `NonTextBlocked`), no row is written. `acomplete`
  records these through `_record_failed_prepare`.
- **Change.** Do the same in `astream`: on a failure in `prepare`, call
  `_record_failed_prepare` (if there is a store) and re-raise.
- **Test.** A streaming request that is too large for the model (reuse the policy from
  `test_size_fail_clearly_and_reroute`) raises `ContextTooLarge` on first iteration, and
  `requests` has one row whose `error` starts with `ContextTooLarge`. That row's `input_json`
  must be empty (refused inputs are never stored).

#### T3. Close SQLite connections
- **Problem.** `with connect(path) as c:` commits but doesn't close the connection
  (`sqlite3` semantics). It is used in `Store.__init__`, `Store.query`, and through
  `store_conn()` in `experiments.py` and `finetune.py`. Python 3.13+ warns about this, and
  cppagent runs on 3.14.
- **Change.** Close every such connection, for example
  `with contextlib.closing(connect(path)) as c, c:` for writes and
  `with contextlib.closing(connect(path)) as c:` for reads. Don't touch the writer thread's
  long-lived connection in `Store._run`.
- **Done when** `grep -n "with connect(\|with store_conn(" -r llm_wrapper` shows no use without
  `closing`, and the suite passes.

#### T4. Fine-tune export must not write damaged examples
- **Problem.** `export_finetune` (`finetune.py`) keeps only messages whose `content` is a
  string. If a prompt has a multi-part message, a tool message or an assistant message with
  `tool_calls`, that message is silently dropped and the rest is exported as if complete.
- **Change.** Skip the whole example in those cases and count it in a new
  `ExportStats.skipped_non_text`. Print it in `cmd_export_finetune` and include it in the
  manifest (it is, automatically, once it is a stats field).
- **Test** (`tests/test_finetune.py`). Seed one normal call and one whose input has a
  multi-part user message. Export: one example written, `skipped_non_text == 1`.

#### T5. Two small experiment fixes
- **Budget skips are invisible.** In `Runner._one` (`experiments.py`), when `budget_usd` is
  already spent the function returns without writing anything, so the report shows a smaller
  `n` with no explanation. Write a result row with `ok: 0` and `error: "budget exhausted"`
  instead. In `report()`, count these separately as `skipped_budget` (not as `errors`), show
  the count in `format_report`, and make `promotion_snippet` ignore them when it checks
  `errors`. Also say in the module docstring and in `README.md` that `budget_usd` caps arm
  calls, that the judge has its own cap (`judge.max_usd`), and that up to `concurrency` calls
  already in flight can overshoot.
- **Unsafe role in the proposal.** `promotion_snippet` builds `metadata.role == '<role>'`. If
  the role doesn't match `^[\w.-]+$`, don't emit a route; return a comment line saying the
  condition must be written by hand. For a normal role, check the condition with
  `expr.compile_expr` before returning it.
- **Tests.** A spec with `budget_usd: 0` produces rows with `error == "budget exhausted"` and
  no model calls. A role of `it's` yields no `routes:` text.

### Phase 2: faithful replay and offline tooling

#### T6. Record `response_format` and replay it
- **Problem.** `_record` stores `"response_format": true` instead of the schema, and
  `Runner._one` drops `response_format` on replay. So a structured call is replayed as a
  free-text call, which is exactly the unfaithful replay that made the first experiments noisy.
- **Change.**
  1. In `_record` (`pipeline.py`): if `record_inputs` is true and the caller's
     `response_format` is a dict, store the dict in `params_json`. Otherwise store `true` as now.
  2. In `Runner._one` (`experiments.py`): forward a stored dict `response_format`; drop it
     only when the stored value is not a dict.
  3. Let an arm remove any recorded param: a key in `arm.params` whose value is `null` deletes
     that param. Document this in the module docstring and `README.md`.
- **Tests.** Update `test_replay_uses_recorded_params` in `tests/test_experiments.py`: a stored
  `true` is dropped; a stored dict is forwarded unchanged; an arm with
  `params: {response_format: null}` sends none. Add a pipeline test that a dict
  `response_format` ends up in `params_json` as the same dict.

#### T7. Redact `response_format` schemas like tool schemas
- **Problem.** `redact_payload` covers messages, tool-call arguments, tool results and tool
  schemas. A `response_format` JSON schema is sent as is, and the leak check doesn't look at
  it. A private value in a schema `description` or `enum` would leave unredacted.
- **Change.**
  1. In `Wrapper._guard` (`pipeline.py`), accept the caller's `response_format`. For an
     untrusted target, when it is a dict, redact string values under `description` and inside
     `enum` lists with the existing `_redact_json_strings` helper (`privacy/egress.py`), using
     the same vault, detector and report. Return the redacted schema.
  2. Include the redacted schema in the payload given to `leak_check`.
  3. Send the redacted schema to the model, but record the caller's original in `params_json`
     (T6). Add a field to `Prepared` for the original rather than reading it back from
     `p.params`.
  4. When the size check reroutes to a model with different trust, `_guard` already runs again;
     make sure the schema goes through it again too.
- **Tests.** A schema with an email in an `enum`: the outbound schema has a placeholder, the
  model's reply containing that placeholder is restored, `params_json` holds the original
  schema, and a trusted model receives the schema untouched.

#### T8. Dry-run experiments
- **Goal.** See what an experiment would do, and roughly cost, before spending anything.
- **Change.** Add `--dry` to `wrapper experiment run`. Implement `dry_run(wrapper, store, spec_path)`
  in `experiments.py`:
  - Sample exactly as a real run does.
  - For each sample and arm, build the messages and params exactly as `Runner._one` does, then
    call `wrapper.prepare(messages, params, tag, force_model=arm.model, source="experiment")`.
    `prepare` makes no model call and writes nothing.
  - Catch `ContextTooLarge`, `EgressBlocked` and `NonTextBlocked` and count them by type as
    "would fail".
  - Input tokens: `size.count_tokens(model, p.messages, p.tools)` (not `p.size.tokens`, which
    is an estimate on the fast path). Output tokens: the sample's logged `completion_tokens`.
  - Cost: `litellm.cost_per_token(model=..., prompt_tokens=..., completion_tokens=...)`; if the
    model has no price in LiteLLM, show `-`.
  - Print, per arm: samples, would-fail counts, rerouted count, mean placeholders, mean input
    tokens, estimated mean and total cost. Add the judge's worst case: `judge.max_usd`.
    Label the numbers as estimates (no cache discount, logged output length).
  - Write nothing to `experiments`, `experiment_results` or `requests`.
- **Tests.** With a `FakeModel`: after a dry run `fake.calls == []`, no `experiment` rows
  exist in `requests`, and the returned report has the per-arm fields above.

#### T9. Scorer plugins and a primary scorer
- **Goal.** Let a caller score answers with its own check (schema validation, a compile check,
  a domain rule) and promote on that instead of the judge.
- **Change** (`experiments.py`).
  1. `Spec.scorers` accepts the three built-in names or `python:<module>:<function>`.
     Change the field type to `list[str]` and validate each entry in a field validator.
  2. Loading: `importlib.import_module(module)` after putting the spec file's folder first on
     `sys.path`. The function is called as `fn(sample, answer)` and returns a float in 0..1 or
     `None`. `sample` is a dict with `id`, `messages`, `reference` (the logged answer text),
     `meta` and `params`. The scorer's name is the function name; reject a spec where two
     scorers share a name or a plugin uses a built-in name.
  3. A plugin that raises, or returns something outside 0..1, gives `None` for that sample.
     Keep the first three distinct error messages per arm for the report. Never abort the run.
  4. Store plugin scores in a new `scores_json TEXT` column of `experiment_results`
     (a JSON object, name → score or null). Add the column with the "ALTER if missing" pattern
     next to `judge_error`.
  5. `report()`: for each plugin scorer give mean, bootstrap 95% CI and the paired difference
     with the control. `_paired` reads `r[key]`; give it a way to read a plugin score.
     `format_report` prints them like the judge lines.
  6. `Spec.primary: str = "judge"`. It must name a scorer in `scorers` (validate; `judge` stays
     valid as the default only if `judge` is in `scorers`, otherwise require the user to set
     `primary` when they want promotion). `promotion_snippet` rules:
     - primary is `judge`: unchanged.
     - primary is a plugin: at least `min_judged` scored samples, and the arm is not
       significantly worse than the control (the paired 95% CI's upper bound is ≥ 0).
     - in both cases the cost and `json_valid` rules stay.
  7. `README.md`: document plugins with a 10-line example, and state plainly that a spec file
     runs Python code, so only run specs you trust.
- **Tests.** Write a scorer module into `tmp_path` and reference it from a spec in the same
  folder. Check: scores stored in `scores_json`; report has mean, CI and paired difference; a
  raising scorer yields `None` and a reported error; promotion on a plugin primary works; a
  name clash is rejected at load.

#### T10. Sample size on the command line, and an honest label
- **Change.** Add `--limit N` to `wrapper experiment run`; it overrides `dataset.limit`.
  In `format_report`, when any arm has fewer than 100 scored samples on the primary scorer,
  add the line `exploratory: fewer than 100 scored samples; only large differences are detectable`.
- **Tests.** The override reaches `sample()`; the line appears for a small run.
- **Then** mark "`--samples 100` defaults for decisions" in `PLAN.md` §7 as done, describing
  what was built.

### Phase 3: tuning arms

#### T11. Prompt and context variants
- **Goal.** Arms that change the prompt, not only the model (plan item M5c).
- **Change** (`Arm` in `experiments.py`).
  - `system: str | None` replaces the content of the first system message (or inserts one at
    the front if there is none).
  - `system_file: str | None` does the same from a file, resolved relative to the spec file.
    `system` and `system_file` together are an error.
  - `system_suffix` (exists) is applied after the replacement.
  - `transform: str | None`, written `python:<module>:<function>`, loaded with T9's loader. It
    is called as `fn(messages) -> messages` and applied last. This is the hook for context
    strategies such as trimming old turns.
  - **The judge must see the caller's original request**, not the modified prompt. Today
    `_one` passes the modified `messages` to `_judge`. Pass the original messages. Otherwise an
    arm could win by rewriting the question.
  - Add `examples/transforms.py` with one example, `keep_first_and_last(messages)`, which keeps
    system messages, the first user message and the last four messages.
- **Tests.** Each option changes the outbound messages as specified; the judge prompt contains
  the original system text and not the replacement; `system` plus `system_file` is rejected.

### Phase 4: outcome-based comparison

Why this exists: for an agent, the score that matters is whether the task succeeds. That can't
be computed for a single recorded call, because traces don't hold the project state at the
time of the call. It can be measured per run: run the caller's own command several times per
arm, each arm under its own policy, and compare success rates and cost per successful run.

#### T12. Run-level A/B harness: `wrapper ab run SPEC` and `wrapper ab report ID`
- **New file** `llm_wrapper/ab.py`, plus CLI wiring in `cli.py`.
- **Spec** (pydantic, `extra="forbid"`):
  ```yaml
  id: fastlanes-stage2
  command: ["bash", "examples/cppagent_ab/run.sh", "{run_dir}", "{policy}"]
  cwd: .                          # optional; relative to the spec file
  env: {}                         # optional extra environment for every run
  arms:                           # first arm = control
    - {id: control, policy: policies/base.yaml}
    - {id: luna-planner, policy: policies/base.yaml, overlays: [policies/planner_luna.yaml], env: {}}
  runs: 5                         # per arm
  success: {exit_code: 0}         # or {stdout_regex: "^Success: True"}; exactly one of the two
  timeout_s: 1800
  budget_usd: 2.0
  out_dir: var/ab                 # relative to the spec file
  ```
- **Behavior.**
  1. For each arm, load the policy with its overlays (`policy.load_policy`) and write
     `LoadedPolicy.effective_yaml` to `<out_dir>/<id>/<arm>.policy.yaml`. This text keeps
     `${ENV:...}` references unresolved, so no secret is written.
  2. Run order: run 0 of every arm, then run 1 of every arm, and so on, so drift over time
     hits all arms equally. One run at a time.
  3. For each run create `<out_dir>/<id>/<arm>/<i>/` and start the command with
     `subprocess.run` (argv list, never a shell string). Replace `{run_dir}`, `{policy}`,
     `{arm}` and `{i}` in each argv item. Environment: the current environment, then spec
     `env`, then arm `env`, then `WRAPPER_POLICY=<arm policy file>`, `WRAPPER_DB=<the --db path, absolute>`
     and `WRAPPER_RUN_TAG=<id>:<arm>:<i>`. Write stdout and stderr to `stdout.log` and
     `stderr.log` in the run folder. A timeout counts as a failed run.
  4. **Tagging calls.** Add a `run_tag` column to `requests` (through `MIGRATIONS` in
     `storage.py`, plus an index). `Wrapper.__init__` reads `WRAPPER_RUN_TAG` once;
     `_record` and `_record_failed_prepare` write it. With no variable set the column is NULL.
  5. After each run, read that run's calls from `requests` by `run_tag`: number of calls,
     errors, tokens, cost. Store one row per run in a new table
     `ab_runs(experiment_id, arm_id, run_index, tag, success, exit_code, timed_out, duration_s, calls, errors, prompt_tokens, completion_tokens, cost_usd, started)`,
     and the spec in `ab_experiments(id, spec_yaml, started, finished)`. Re-running an id
     replaces its earlier rows.
  6. Before each run, if the cost recorded so far for this experiment is at or above
     `budget_usd`, stop and record the remaining runs as skipped (not as failures).
  7. **Report**, per arm: runs, successes, success rate with a Wilson 95% interval, mean cost
     per run, cost per successful run (total cost ÷ successes, `-` when there are none), mean
     calls, mean duration. Against the control: the difference in success rate with a
     bootstrap 95% CI (resample each arm's runs independently, seeded) and whether the CI
     excludes 0. End with a line stating that with few runs only large differences are
     detectable.
- **Tests** (`tests/test_ab.py`, offline). Add `tests/ab_child.py`, a small script that:
  calls `llm_wrapper.register()` (policy and database come from the environment), replaces
  `handler.wrapper.acompletion` with `tests.conftest.FakeModel()`, makes two
  `litellm.completion(model="wrapper/auto", ...)` calls, and exits with the code in the
  `CHILD_EXIT` environment variable. The test writes a policy, runs a spec with two arms
  (`env: {CHILD_EXIT: "0"}` and `{CHILD_EXIT: "1"}`) and two runs each, with
  `command: [sys.executable, "-m", "tests.ab_child"]` and `cwd` at the repository root. It must
  also set `WRAPPER_PRIVACY_KEY` (64 hex characters) in the spec `env`, so the child never
  creates a key file in the home folder. Assert: four `ab_runs` rows; success 2 of 2 and 0 of
  2; two calls and a cost above 0 per run; `requests.run_tag` matches; the report shows both
  rates; the policy file written for an arm contains no resolved secret (use a policy with a
  `${ENV:X}` reference in a custom pattern or similar field and check `X`'s value is absent).
  Also test the budget stop and the `stdout_regex` success rule.

#### T13. Example files for running cppagent under the harness (files only; no live run)
- Add `examples/cppagent_ab/README.md`, `run.sh`, `ab.yaml` and `policies/base.yaml`.
- `run.sh RUN_DIR POLICY`: copies `examples/cppagent_ab/cfg` into `RUN_DIR/cfg`, then runs
  `scripts/run_cppagent_with_wrapper.py POLICY --config RUN_DIR/cfg/task.json --model wrapper/auto --no-cache`
  with cppagent's interpreter (`$CPPAGENT/.venv/bin/python`) and
  `PYTHONPATH=<this repo>:$CPPAGENT/usecase`. `cfg/task.json` sets `"output_dir": "../work"`,
  so each run gets its own `RUN_DIR/work`. Read `usecase/run_task.py` in cppagent (read-only)
  to confirm the flags and how it reports success (it prints `Success: True` or `Success: False`),
  and choose the `success` rule from what you find.
- The README must say: what a run costs (a stage-2 run made 11 calls for $0.023 on
  2026-11-10; stage 1 also calls the model), that credentials come from the environment or a
  `.env` in the working folder, and the open point in section 6 item 1 about the starting state.
- **Done when** the files exist and `wrapper ab run examples/cppagent_ab/ab.yaml` would start
  correctly, judged by reading. **Do not run it.** It calls real models.

### Phase 5: gateway cost levers (offline)

#### T14. Provider cache markers and cache-write pricing (finishes plan item M8)
- **Change.**
  1. Markers. Anthropic-style callers put `cache_control` on message content parts. Add a
     test that after `prepare`, every `cache_control` key is still present, on the same part,
     with the same value, while the text is redacted. If the test fails, fix `redact_payload`.
  2. Hint placement. `add_hint` (`privacy/egress.py`) appends the hint to the first system
     message only when its content is a string; with list content it inserts a new system
     message at the front, which shifts the cached prefix. For list content, append one text
     part `{"type": "text", "text": HINT}` to the end of that message's parts instead.
  3. Cache-write tokens. `_usage_of` (`pipeline.py`) reads cached (read) tokens only. Also
     read cache-creation tokens. In the installed LiteLLM, `Usage` exposes
     `cache_creation_input_tokens` and `prompt_tokens_details.cache_creation_tokens`; check
     `.venv/lib/python3.12/site-packages/litellm/types/utils.py` rather than trusting this
     note. Pass them to `litellm.cost_per_token(..., cache_creation_input_tokens=...)`, add a
     `cache_write_tokens` column through `MIGRATIONS`, and include it in `_add_usage` and
     `_litellm_usage`.
- **Tests.** The three points above, with a `FakeModel` reply whose usage carries the fields
  (follow `test_prefix_byte_stable_across_turns_and_cached_tokens_recorded`). That existing
  test must still pass unchanged.

#### T15. Opt-in fallback on configuration errors (plan item M10)
- **Why.** The fastlanes traces hold 58 calls that failed on configuration: a rejected
  `response_format`, a model not found, bad credentials.
- **Change.**
  1. Policy (`policy.py`): `fallback: {enabled: false, on: [bad_request, not_found, auth], to: null}`.
     `to: null` means `models.default`. Validate `on` entries; apply the existing self-call
     check to `to`.
  2. `Wrapper.acomplete`: when the model call raises `litellm.BadRequestError` (includes
     `UnsupportedParamsError`), `litellm.NotFoundError` or `litellm.AuthenticationError`, and
     the matching kind is in `on`, and fallback is enabled, and the request's model differs
     from the fallback target, and the model was not forced (`force_model` is None): prepare
     the request again for the target (redaction must be redone, because trust can differ)
     and call once more. One fallback at most.
  3. Never fall back on `litellm.ContextWindowExceededError` (it subclasses `BadRequestError`;
     check for it first), timeouts or rate limits.
  4. Record one `requests` row for the final outcome. Put `fallback_from` (the first model) and
     `fallback_error` (the exception type name) into the `wrapper` info dict, and add a
     `fallback` span.
  5. Streaming is not covered; say so in `README.md`.
- **Must not change.** With fallback disabled (the default), or when the failing model is
  already the default, the provider's error reaches the caller exactly as today. cppagent
  relies on seeing a `response_format` rejection itself, and that was verified live.
- **Tests.** Enabled and routed model fails with `BadRequestError` → the default answers, one
  row, `fallback_from` set. Disabled → the error propagates. Failing model is the default →
  propagates. `ContextWindowExceededError` → propagates. Target trusted differently from the
  first model → the second call's payload is redacted (or not) according to the target.

## 6. Stop here: work that needs the user's go-ahead

Don't start any of these. Report that section 5 is done, then ask about them one at a time
with the cost.

| # | Work | Why it needs approval | Rough cost |
|---|---|---|---|
| 1 | Live A/B on fastlanes with the T12 harness, 5 runs per arm | Calls real models. Also needs a decision on the starting state: each run should start from the same clean point (for example stage 1 from an empty folder with a smaller `big_rows`, or a saved snapshot after stage 1) | About $0.50–1.00 and 20–40 minutes |
| 2 | Experiment with a strong arm (for example `openai/gpt-5.3-codex`) on planner and repair, about 100 samples, then `export-finetune --select experiment:ID:ARM` | Calls real models | About $3 |
| 3 | cppagent sends `metadata={"role", "session_id", "task_id"}` on each call (plan items M4/M5d) | Edits the user's other repository, which has uncommitted changes | Free |
| 4 | Proxy mode (plan item M3): run under the LiteLLM proxy and test that spend is counted once | Needs `litellm[proxy]` installed and a running server | Free, but a new dependency |
| 5 | Remove the "Task was destroyed but it is pending" warning at process exit after sync calls | Only reproducible with a real network call (aiohttp cleanup on the background loop in `provider.py`) | A few cents |
| 6 | Submit a fine-tuning job | Uploads data to a provider and costs money; fastlanes alone has too few examples | Provider pricing |
| 7 | `git init` and commits | The user hasn't asked for a repository | Free |

## 7. Out of scope

Don't build these, even if they look close: request decomposition or planning, response-triggered
actions, an admin UI, Postgres, OpenTelemetry export, learned routing (plan v4), local name
detection, and any change to the detector regexes that isn't forced by a failing test.

## 8. Reporting

After each task, append a line to the log below: date, task id, what changed in one sentence,
and the test count. If you had to deviate from a task's wording, say how and why in that line.

When section 5 is finished (or you stop early), give the user:
1. which tasks are done, and which are not and why
2. the final test count and the command output line (`N passed`)
3. anything you found that looks wrong but that no task covers (don't fix it; list it)
4. the first question from section 6

### Progress log
- 2026-11-11: brief written. Before handoff: `sample()` no longer picks up rows written by
  earlier experiments (their replays and judge prompts carried the caller's role and were
  being sampled as caller traffic); examples and scripts moved from scratch space into
  `examples/`, `scripts/` and `var/`. 65 tests pass.
- 2026-11-11: T1-T5 done. Vault value is `NULL` when `record_inputs` is false; `astream` records refused
  requests; connections are closed (`store_conn` is now a closing context manager, `Store` uses
  `contextlib.closing`); export skips non-text examples; budget skips are recorded (`skipped_budget`)
  and unsafe roles get no route. 70 tests pass. Local git repo created (commit 890d28d); no remote (no `gh`
  or token on this machine).
- 2026-11-11: T6-T10 done. `response_format` recorded in `params_json` (original schema when `record_inputs` is
  true) and replayed; arm param `null` removes a param; schemas are redacted and leak-checked like tool
  schemas (`redact_response_format`, `Prepared.caller_response_format`); `experiment run --dry`; scorer
  plugins (`scores_json` column, `Spec.primary`, `RESULT_COLUMNS` + `ensure_schema`); `--limit` and the
  "exploratory" label. Deviation: `Spec.scorers` stays a list of strings and a default `primary: judge` is
  allowed without a judge scorer (promotion then finds nothing). 79 tests pass.
- 2026-11-11: T11 done. Arms take `system` / `system_file` / `system_suffix` / `transform`; `replay_request`
  returns the caller's original messages as a fifth value and the judge reads those; `ReplayContext` holds
  system texts and loaded transforms; `examples/transforms.py` added. Note for later tasks: with an untrusted
  model the gateway appends its placeholder hint to the system message (or inserts one), so tests compare
  prefixes. 82 tests pass.
- 2026-11-11: T12-T13 done. `llm_wrapper/ab.py` (`wrapper ab run|report`, tables `ab_experiments` / `ab_runs`,
  `requests.run_tag` via `MIGRATIONS` plus an index created in `_migrate`), `tests/ab_child.py`, `tests/test_ab.py`;
  `examples/cppagent_ab/{ab.yaml,run.sh,README.md,policies/}` written, NOT run. Deviations: `ab_runs` has an extra
  `skipped` column; the second arm routes every call to gpt-6-luna (cppagent sends no `metadata.role` until M4, so a
  per-role route could not match); success uses `^Success: True` on stdout (cppagent prints it once, for the last stage
  it ran); commands run in their own process group so a timeout kills child processes too. The suite now takes about a
  minute because the A/B tests start child Python processes. 89 tests pass.
