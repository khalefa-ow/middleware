# llm_wrapper: a platform to optimize and tune LLM usage (prototype)

Loop: **record** (gateway) → **experiment** (replay recorded calls as variants) → **compare** (cost, latency, quality) → **promote** (a policy route you approve).

A LiteLLM custom provider: callers switch to `model="wrapper/auto"`. Every request is
routed by policy rules to a real model, redacted before it leaves the machine, size-checked,
restored on the way back, and traced to a local SQLite file.

## What works
| Area | Status |
|---|---|
| Provider | `wrapper/auto` via `litellm.completion` / `acompletion`, sync + async, streaming |
| Caller params | forwarded unchanged (`temperature`, `max_tokens`, `stop`, `seed`, `tools`, `response_format`, ...) |
| Routing | policy `routes` with safe conditions on `metadata.*` and `request.*`; `metadata.wrapper_route` override |
| Privacy | email, phone (needs context), card (Luhn + prefix + grouping/context), SSN, API keys, custom + allow patterns; code blocks skipped; HMAC placeholders `<EMAIL_1a2b3c4d>` scoped per conversation; tool schemas, tool-call args and tool results redacted; leak check blocks the call; non-text `trusted_only`; trusted models get real values |
| Restore | tolerant (case, missing brackets) but only vault ids; `response_values: input_only`; streaming-safe buffer; tool-call arguments JSON-escaped |
| Size | token count after redaction vs. model limit; reroute to `large_context`; fail clearly with numbers; continuation of cut-off plain text only when the caller set no `max_tokens` and no tools/format |
| Safety | self-call guard (policy validation + runtime depth); internal calls tagged `metadata.wrapper_internal` |
| Storage | SQLite WAL, background writer, 0600 file; requests, spans, egress/size events, redactions, vault, policy versions |
| Ops | `wrapper init / validate / import-cppagent / import-chat / analyze / bench / delete / purge / health / show` |

Not built yet: outcome scorers (e.g. build-and-compare for cppagent), dry-run replay, submitting
fine-tuning jobs, learning, proxy spend de-duplication test.

## Use
```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/wrapper init policy.yaml          # commented starter policy
.venv/bin/wrapper validate policy.yaml
```
```python
import litellm, llm_wrapper
llm_wrapper.register("policy.yaml")          # or WRAPPER_POLICY=...; DB: WRAPPER_DB=... or storage.path
litellm.completion(model="wrapper/auto", messages=[...], metadata={"role": "planner", "session_id": "run-1"})
```
LiteLLM proxy `config.yaml`:
```yaml
litellm_settings:
  custom_provider_map:
    - {provider: wrapper, custom_handler: llm_wrapper.proxy_handler}
model_list:
  - {model_name: wrapper/auto, litellm_params: {model: wrapper/auto}}
```
The privacy key comes from `WRAPPER_PRIVACY_KEY` (hex) or `~/.llm_wrapper/key` (created 0600).

### cppagent
cppagent calls `litellm.completion(model=self.model, ...)`, so set its model to `wrapper/auto`.
To route by role, cppagent must pass its role in `metadata` (e.g. `extra_params={"metadata": {"role": ...}}`
per call); until then routes can match on `request.system`.

## Phase-0 analysis
```bash
.venv/bin/wrapper --db fl.db import-cppagent ../cppagent-usecases/fastlanes
.venv/bin/wrapper --db fl.db analyze            # or --json
```
Public or other OpenAI-style logs (one conversation per JSONL line; each assistant turn becomes a call):
```bash
.venv/bin/wrapper --db pub.db import-chat swe.jsonl --messages-field trajectory --id-field trajectory_id \
    --outcome-field resolved --kind agent --source public_swe
.venv/bin/wrapper --db pub.db analyze --source public_swe --price-model deepseek/deepseek-chat --cached-price-ratio 0.26
.venv/bin/wrapper --db fl.db bench               # added latency of routing + redaction + size check
```
Reports spend by role/model at current LiteLLM prices, latency, parse rates, prompt-prefix
reuse (what provider prompt caching would save), duplicate prompts, size distribution,
errors, and a redaction dry run with masked samples to check for false positives.

## Experiments
```yaml
# planner.yaml
id: planner-cheaper
dataset: {source: own_import, role: planner, limit: 20, seed: 1}   # also: model, only_ok
arms:                                  # first arm = control; costs are compared to it
  - {id: control, model: deepseek/deepseek-chat}
  - {id: luna, model: openai/gpt-6-luna, params: {temperature: 0}, system_suffix: "Be concise."}
scorers: [json_valid, similarity, judge]
judge: {model: openai/gpt-5.3-codex, max_usd: 0.6}                # blind, order-randomized
budget_usd: 0.5
concurrency: 6
# primary: judge                       # the scorer the promotion bar uses (judge or a plugin's function name)
```
```bash
.venv/bin/wrapper --db fl.db experiment run planner.yaml     # report + promotion proposal (a policy route)
.venv/bin/wrapper --db fl.db experiment report planner-cheaper
.venv/bin/wrapper --db fl.db experiment run planner.yaml --dry     # no model calls: refusals, reroutes, cost estimate
.venv/bin/wrapper --db fl.db experiment run planner.yaml --limit 100   # 100+ scored samples for decisions
```
Every arm call goes through the gateway (redaction, size checks, tracing; source `experiment`).
Replays reuse the caller's recorded params when the gateway recorded them, including a
`response_format` schema (an arm param set to `null` removes that param, e.g. `params: {response_format: null}`).
Reports below 100 scored samples say "exploratory": only large differences are detectable.
`--dry` runs the gateway's checks on every sample and arm (nothing is sent or stored) and prints
what would be refused or rerouted and an estimated cost (list price, no cache discount).

`budget_usd` caps the arm calls only; the judge has its own cap (`judge.max_usd`). Calls already in
flight when a cap is reached still finish, so spend can overshoot by up to `concurrency` calls.
Samples skipped because the budget ran out get a result row (`error: budget exhausted`) and are
reported as `skipped_budget`, not as errors.

### Your own scorers

A scorer plugin is a function `fn(sample, answer) -> float | None` (0..1; `None` = not applicable).
`sample` has `id`, `messages`, `reference` (the logged answer), `meta` and `params`.

```python
# checks.py (next to the spec)
import json
def valid_plan(sample, answer):
    try:
        return 1.0 if json.loads(answer).get("steps") else 0.0
    except Exception:
        return 0.0
```
```yaml
scorers: [json_valid, "python:checks:valid_plan"]
primary: valid_plan        # promotion uses this scorer instead of the judge
```
Each plugin gets a mean, a bootstrap 95% CI and a paired difference against the control. A plugin that
raises or returns something outside 0..1 scores `None` for that sample; the first errors appear in the report.
**A spec runs Python code from its folder: only run specs you trust.**

### Fine-tuning datasets

```
wrapper export-finetune planner-v1 --role planner --require-success --out ft/
wrapper export-finetune planner-distill --role planner --select experiment:planner-cheaper:strong --out ft/
```

Writes `NAME.train.jsonl`, `NAME.holdout.jsonl` (OpenAI chat fine-tuning format) and a manifest.
Prompts and answers are redacted by default (same value, same placeholder in prompt and answer).
The split is stored, so after tuning, evaluate on unseen prompts with `dataset: {holdout_of: NAME}`;
the command prints a ready base-vs-tuned spec. Examples with images, tool messages or tool calls
are skipped whole (counted as `non-text`), never exported partly.

With `storage.record_inputs: false` the gateway keeps no prompts, answers or vault values, only
hashes (so `wrapper delete --value` still finds the requests).

## Tests
`.venv/bin/pytest -q` (79 tests, no network: the model is faked at `Wrapper.acompletion`).
