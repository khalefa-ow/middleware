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
```
```bash
.venv/bin/wrapper --db fl.db experiment run planner.yaml     # report + promotion proposal (a policy route)
.venv/bin/wrapper --db fl.db experiment report planner-cheaper
```
Every arm call goes through the gateway (redaction, size checks, tracing; source `experiment`).
Replays reuse the caller's recorded params when the gateway recorded them.

### Fine-tuning datasets

```
wrapper export-finetune planner-v1 --role planner --require-success --out ft/
wrapper export-finetune planner-distill --role planner --select experiment:planner-cheaper:strong --out ft/
```

Writes `NAME.train.jsonl`, `NAME.holdout.jsonl` (OpenAI chat fine-tuning format) and a manifest.
Prompts and answers are redacted by default (same value, same placeholder in prompt and answer).
The split is stored, so after tuning, evaluate on unseen prompts with `dataset: {holdout_of: NAME}`;
the command prints a ready base-vs-tuned spec.

## Tests
`.venv/bin/pytest -q` (65 tests, no network: the model is faked at `Wrapper.acompletion`).
