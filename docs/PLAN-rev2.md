# LLM Wrapper: Build Plan (rev 2)

Rev 2 applies all 37 items from REVIEW.md. The last section, "Review changes", lists where each item ended up.

## Goal
A Python package that registers with LiteLLM as a custom provider (`model="wrapper/auto"`). It sits between callers and real models and:
- keeps private data from leaving the machine
- plans requests by splitting or combining them into steps, or passes them straight through
- decides for each step whether code, an agent or a model handles it, choosing the option with the best expected cost
- keeps every call within model size limits
- lets hooks change requests and responses, and lets responses trigger actions
- records full traces locally, with retention and deletion
- learns from past interactions, including imported logs and public traces
- is controlled by a declarative YAML policy with comments

Callers switch by changing the model name to `wrapper/auto`. The LiteLLM proxy needs the provider registered in its config.

## Decisions
| Area | Choice |
|---|---|
| Language | Python package, many modules. **Async core**; `completion()` is a thin sync wrapper used only outside the proxy |
| Integration | LiteLLM custom provider. Internal model calls go through a dedicated LiteLLM `Router` with proxy callbacks off and `metadata.wrapper_internal: true`. Spend is reported once, on the outer call |
| Database | Local SQLite (WAL mode, one background writer), behind a storage interface so Postgres can replace it. SQLCipher optional |
| Privacy | DB stores real values, with retention and deletion. An egress guard redacts everything leaving the machine. Only trusted targets get real values |
| Restore point | Inside the wrapper everything works on placeholders. Real values return only in the final response and in inputs to local or trusted code and actions |
| Placeholders | Keyed hash, 8 hex chars minimum (`<EMAIL_a3f91c0d>`), key scope `conversation` by default, collision check against the vault |
| Provenance | Every piece of text and every vault value records where it came from. Provenance gates restores and risky actions |
| Fast path | Two-stage: a cheap pre-check (rules + size, no model call) passes most requests straight through. Caller-constrained requests always pass through |
| Caller tools | Caller's `tools`/`tool_calls` always go back to the caller, with placeholders restored in arguments; tool schemas are redacted on the way out; the wrapper runs only policy tools |
| Caller instructions | The caller's system prompt goes into every LLM step and the combine call; wrapper instructions come after it, marked |
| Judge | Hybrid: rules → similarity → small classifier → default. Returns eligible candidates, not one choice |
| Optimizer | Chooses among candidates by expected cost (money + time + failure cost) under quality and safety limits. The default model is always eligible |
| Pattern | Layered key: rule id, else cluster id, plus input-size bucket. Versioned |
| Quality signal | Validators, behavior signals (needs identity), sampled review by a stronger model, logged answers as reference, optional user rating |
| Learning | Seeded from imported logs and public traces; only own data can confirm costs or moves to code; stats per exact model version |
| Policy | YAML with comments (`ruamel.yaml` round-trip), schema-validated, layered, versioned. Conditions: small custom evaluator on Python `ast` with a strict allowlist, `google-re2` for `matches`. Secrets by reference only |
| Message size | Every outbound model call is measured against model limits, with a per-model correction factor; oversized input is reduced, split or rerouted; lossy strategies need permission; cut-off text is continued, cut-off tool calls are retried |
| Hooks vs. actions | Hooks change data in-process. Actions have side effects outside the request |
| Actions | Triggered by policy triggers and policy tools only, never by the planner; risk levels; approvals; background queue |
| Delivery | Six releases, each usable on its own and gated by acceptance criteria (see Versions) |

## Versions
| Version | Scope | Why this order |
|---|---|---|
| **v1: Safe pass-through** | Provider, policy loader, tracing, egress guard + restore, size budget, fast path, usage reporting, retention | Useful and safe from day one; starts collecting data |
| **v1.5: Import + replay** | Importers for your JSONL logs and sampled public traces, labeling run, dry and live-sample replay, benchmark | Gives learning and the optimizer thousands of examples before live use |
| **v2: Rules** | Pre-check, rule planner (graph), code handlers, agent routing, hooks, judge tier 1, optimizer v1 (declared costs) | Saves money on obvious cases with no learning risk |
| **v3: Quality signal** | Validators, behavior signals, sampled review, outcomes, logged-answer comparison, optional name detection | Learning can't be trusted without it |
| **v4: Learning** | Pattern clustering, similarity tier, classifier tier, learned estimates, proposals, learned policy layer, background exploration, LLM planner, full size strategies | Now there is data and a quality signal |
| **v5: Actions** | Triggers, risk levels, approvals, background queue, undo | Riskiest part, built last |

### Acceptance criteria
A version ships only when it meets every target that applies to it. Targets are measured on the replay set and the test suites.

| Metric | Target | From |
|---|---|---|
| Leaks | 0 in the leak, injection and non-text test suites | v1 |
| Correct pass-through | 100% of caller-constrained requests pass through with payload unchanged (except redaction) | v1 |
| Fast-path overhead | p95 under 30 ms added (excluding the model call) | v1 |
| Usage reporting | spend counted exactly once in proxy mode | v1 |
| Restore | 100% of vault placeholders restored in text and tool arguments in the restore suite | v1 |
| Redaction false positives | under 1% of redactions flagged wrong in the labeled replay sample | v1.5 |
| Pre-check accuracy | wrong pass-through or wrong plan under 5% on the labeled replay set | v2 |
| Cost | at least 15% lower than calling the model directly on the replay set (to confirm, see Open questions) | v2 |
| Quality | not significantly worse than the direct model on reviewed samples (paired test, p < 0.05) | v3 |
| Caller format kept | 100% of planned requests with a caller format return that format | v2 |
| Actions | 0 actions above read-only triggered from untrusted input without approval in the injection suite | v5 |

## Package layout
```
llm_wrapper/
├── __init__.py            # register provider with LiteLLM
├── provider.py            # CustomLLM: acompletion / astreaming; sync completion wraps them
├── config.py              # loads settings + active policy (cached, version-polled)
├── pipeline.py            # orchestrates the request flow (async)
├── types.py               # Step, PlanGraph, Candidate, Decision, Result, Span, Provenance, PatternKey
├── params.py              # caller param propagation (see table)
├── identity.py            # user, session id, conversation-prefix hash
│
├── policy/
│   ├── schema.py          # Pydantic models = the language definition
│   ├── loader.py          # load + merge layers (base, env, overrides, learned), validate
│   ├── expr.py            # ast-allowlist evaluator; RE2 regex; missing field = false
│   ├── secrets.py         # resolve ${ENV:NAME} / ${FILE:path}; never traced
│   ├── compiler.py        # policy → runtime objects
│   ├── versions.py        # versions in DB, diff, rollback, hot reload
│   ├── proposals.py       # learning proposals as policy diffs
│   ├── starter.yaml       # commented starter policy written by `wrapper init`
│   └── cli.py             # init / validate / test / diff / explain / replay / delete / health
│
├── privacy/
│   ├── vault.py           # placeholder ↔ value, type, origin, scope key; collision check
│   ├── redactor.py        # keyed-hash placeholders, key scope, key rotation
│   ├── patterns.py        # email, phone (with context), card (Luhn), keys, custom, allow-patterns
│   ├── names.py           # optional local name/entity detection (v3)
│   ├── restore.py         # tolerant restore, origin filter, unknown-placeholder logging
│   ├── non_text.py        # images/audio/files: pass | block | trusted_only
│   └── egress.py          # guard on every outbound path (incl. tool schemas) + pre-send leak check
│
├── security/
│   └── provenance.py      # source tags on text segments; untrusted-input checks for actions
│
├── planning/
│   ├── precheck.py        # stage 1: rules + size only, no model call
│   ├── rule_planner.py    # safe splitting (no code/quotes, if..then kept, min length)
│   ├── llm_planner.py     # JSON plan from a cheap model, falls back to rules; cannot emit actions
│   ├── graph.py           # steps with depends_on; sequential scheduler for now
│   ├── merge.py           # combine small related asks into one step
│   └── context.py         # caller system prompt + original request + relevant history per step
│
├── patterns/
│   ├── key.py             # rule id | cluster id, + size bucket, + pattern_version
│   └── clustering.py      # periodic clustering of redacted-text embeddings (v4)
│
├── judge/
│   ├── rules.py           # tier 1
│   ├── similarity.py      # tier 2
│   ├── classifier.py      # tier 3
│   └── judge.py           # returns eligible candidates + success chance
│
├── optimizer/
│   ├── catalog.py         # tools + declared cost profiles
│   ├── estimator.py       # predict tokens, time, success per candidate
│   ├── selector.py        # choose plan shape + (tool, size strategy) per step
│   ├── spend_budget.py    # split the request's money/time/token limits across steps
│   └── calibration.py     # predicted vs. actual
│
├── executors/
│   ├── code/              # one file per handler + registry
│   ├── agents.py
│   ├── router.py          # dedicated internal LiteLLM Router: callbacks off, wrapper_internal marker
│   └── llm.py             # calls real models via router; self-call guard; timeouts
│
├── context/               # message size handling
│   ├── token_budget.py    # limits, correction factor per model, margin
│   ├── chunker.py
│   ├── reducer.py         # trim / summarize, gated by allow_lossy
│   ├── map_reduce.py
│   ├── continuation.py    # text and unconstrained JSON only
│   └── blob_store.py
│
├── quality/
│   ├── validators.py      # per-handler and schema checks
│   ├── signals.py         # retries, rephrases, errors, cut-offs (needs identity)
│   ├── reference.py       # compare with logged answers from imports
│   ├── reviewer.py        # sampled review by a stronger model, background
│   └── verdict.py         # combine signals into success + which check decided
│
├── actions/
│   ├── registry.py        # actions, risk, schema, undo
│   ├── triggers.py        # conditions + scope (step / final / both)
│   ├── runner.py          # blocking or queued, retries, idempotency, provenance check
│   ├── approval.py        # pending approvals
│   └── builtin/
│
├── importers/
│   ├── jsonl_logs.py      # your JSONL files (streamed) via a YAML field mapping
│   ├── openai_jsonl.py    # OpenAI-style chat logs (most public datasets convert to this)
│   ├── litellm_logs.py
│   ├── public/            # adapters per public dataset; sample size, language filter, dedup
│   └── labeler.py         # strong-model labeling; output becomes proposals only
│
├── replay/
│   ├── runner.py          # dry mode and live-sample mode
│   └── report.py          # savings, routing changes, failures, leaks, redaction FPs, split errors
│
├── hooks.py               # request, step-response and final-response stages
├── combine.py             # keeps the caller's format
├── tasks.py               # one background task queue: writer, reviewer, exploration, action worker
├── retention.py           # nightly purge, delete by request/user/value
├── metrics.py             # overhead, pass-through rate, tiers, leaks blocked, strategies, queue depth
├── storage/               # base.py, sqlite.py, writer.py (flush on shutdown), migrations/
├── tracing/tracer.py      # never records resolved secrets
└── learning/
    ├── stats.py           # per pattern + tool + exact model version, weighted by source and recency
    ├── updater.py         # writes to the learned policy layer
    └── jobs.py            # runs outside requests

tests/
├── test_redaction.py      ├── test_egress.py         ├── test_leaks.py
├── test_restore.py        ├── test_provenance.py     ├── test_injection.py
├── test_caller_tools.py   ├── test_fast_path.py      ├── test_precheck.py
├── test_params.py         ├── test_system_prompt.py  ├── test_size.py
├── test_planner.py        ├── test_judge.py          ├── test_optimizer.py
├── test_quality.py        ├── test_policy_merge.py   ├── test_expr.py
├── test_secrets.py        ├── test_actions.py        ├── test_importers.py
├── test_streaming.py      ├── test_pipeline.py       ├── test_retention.py
├── test_proxy_spend.py    # proxy mode: spend counted once
├── test_concurrency.py    # concurrent requests through the proxy
├── benchmark/             # replay set from your logs: wrapper vs. direct model
└── fixtures/              # incl. injection strings in documents and tool results
```

## Request flow
1. LiteLLM calls `provider.acompletion(messages, **params)` (or the sync `completion`, which wraps it). Self-call guard: refuse if the call depth is above 1.
2. Read identity: `user`, `metadata.session_id`, else a hash of the conversation prefix. Trace the input locally (real values).
3. Tag provenance on every text segment: `caller`, `tool_result`, `document`, `agent`, `import`. The caller's own turns are `caller`; tool messages and attached content are untrusted.
4. Build the placeholder vault for this request. Each entry records type, origin (`caller_input` here) and key scope. Non-text content is handled by `privacy.non_text`.
5. Run request hooks (they see placeholders), then trace their changes.
6. **Choose the plan shape (two stages).**
   - **Always pass through** if the caller passed `tools`, a forced `response_format`, `n > 1`, an unknown param, or any format constraint the wrapper can't guarantee after combining.
   - **Stage 1, pre-check** (rules and size only, no model call): likely splittable? code-able parts? too big for one call? If none, pass through now.
   - **Stage 2:** plan, then the optimizer compares pass-through with the planned shapes (split, map-reduce, large model) and picks the best expected cost. Pre-check misses are recorded so its thresholds can be tuned.
7. **For planned shapes:** plan only the latest user message, with history as context. The plan is a graph (`depends_on`), run in order for now. Each step gets:
   - the caller's system prompt, unchanged and first
   - the wrapper's step instruction, after it and clearly marked
   - the original request and relevant history, with its own part marked as the task

   Steps that need real values are marked. The planner can't create actions or tool calls.
8. **Judge + optimize each step.** Compute the step's pattern key. The judge returns eligible candidates with a success chance. Code candidates are limited to handlers approved for that rule-id pattern, and steps needing real values allow only code or trusted targets. The default model is always eligible. The optimizer picks the best (tool, size strategy) pair within the remaining budget. Store the Decision with the policy and pattern versions.
9. **Execute** (internal model calls go through the internal Router, with per-call timeouts):
   - Code or trusted target: gets real values.
   - Other targets: the egress guard redacts (messages and tool schemas), then runs a leak check on the outbound payload before sending.
   - Cut-off text, or JSON the caller didn't constrain, is continued. A cut-off tool call is retried with a larger `max_tokens` or a larger model instead.
   - New private values from code, agents or tool results enter the vault with their own origin.
   - A failed step falls back to the default model once. If that also fails, the policy's `failures` setting decides: fail the request, skip, or return partial results.
10. Run step-response hooks on each step result (placeholders), then check triggers scoped to `step`.
11. Combine results. The combine call includes the caller's system prompt and must follow the caller's format. It goes through size and egress like any other model call.
12. Run final-response hooks on the combined answer (placeholders).
13. Sort tool calls: the caller's tool calls are returned with placeholders restored in their arguments; policy tool calls are run as actions.
14. Check triggers scoped to `final`. An action above read-only is blocked pending approval if the model turn that produced it had untrusted input. Blocking actions run now; actions needing approval are recorded as pending; queued actions go to the worker.
15. **Restore:** swap placeholders back to real values in the response text and tool call arguments. Restore is tolerant (case, missing brackets) but only matches ids in the vault. Only values whose origin is allowed by `response_values` are restored; others stay masked and are logged. Unknown placeholders are left as is, logged, and count against the step's quality.
16. Return a LiteLLM `ModelResponse`. `usage` totals all internal calls; metadata includes a cost breakdown, size strategies used and the fast-path flag. Spend is counted only here.
17. Record outcomes and quality signals through the background task queue. Learning runs later in `learning/jobs.py`, never inside the request.

**What the fast path skips:** planning, judging and combining. It still runs the egress guard, size check, tracing, hooks, restore, actions and usage reporting.

### Caller params for planned requests
| Param | Applies to |
|---|---|
| `max_tokens`, `stop` | the final output (combine, or the last step) |
| `temperature`, `top_p`, `seed` | every LLM step and the combine call |
| `user`, `metadata` | traces, identity and behavior signals; `metadata` can override the objective |
| `timeout` | the request time limit |
| `stream` | the final call only (see Streaming) |
| `tools`, forced `response_format`, `n > 1` | force pass-through |
| any other / unknown param | forces pass-through |

## Privacy and egress
- **Local storage:** real values are stored. Keep the DB on an encrypted disk with user-only file permissions, or turn on SQLCipher.
- **Egress points:** model provider (main, planner, classifier, reviewer, combine, labeler), outside agents, webhooks, embedding APIs, and the caller when running behind a proxy.
- **Egress guard:** redacts right before sending, so data added by hooks, code or earlier steps is caught too. It also redacts caller tool names, descriptions and enum values.
- **Pre-send leak check:** scans each outbound payload for known real values after redaction. On a hit, the call is blocked and logged.
- **Trusted targets:** one list, `privacy.trusted`, covering models and agents (for example a local model). They get real values.
- **Steps needing real values:** marked by the planner or a rule. Only code or trusted targets are eligible. If neither exists, fail clearly instead of sending real data.

**Detection**
- Regex patterns in v1. Optional local name and entity detection in v3, since regex misses names and free-text details.
- Card numbers must pass a Luhn check.
- Phone numbers need context: separators, a leading `+`, or a nearby word like "phone" or "call".
- Text inside code blocks is skipped unless the policy says otherwise.
- The policy can add allow-patterns (for example `ORD-\d+`) that are never redacted.
- Every redaction is logged with its pattern id, so false positives show up in replay.

**Placeholders**
- Keyed hash with at least 8 hex characters: `<EMAIL_a3f91c0d>`. A collision against the vault forces a longer suffix.
- Key scope is a policy setting:
  - `conversation` (default): stable across turns, not linkable across conversations.
  - `global`: the same value matches across requests. Needed only if similarity must match the same entity across requests; it lets the provider link them.
- Key rotation: keys are versioned; old traces keep the key version they used. Rotation changes every new placeholder.
- Outbound prompts carry a short instruction to keep placeholders exactly as written.

**Vault origin and `response_values`**
- Each vault entry stores its origin: `caller_input`, `code_handler:<id>`, `agent:<id>` or `tool_result`.
- `response_values: input_only` (default) restores only `caller_input` values. `allow: [...]` lists extra origins. Values with other origins stay masked and are logged.

**Non-text content:** `privacy.non_text: pass | block | trusted_only`, default `trusted_only`. Non-text content is flagged in traces.

**Imported data:** your logs keep real values locally. Public datasets go through redaction on import.

**Retention and deletion**
- `retention` policy section with a period per table (traces, steps, blobs, summaries, imports, vault).
- `wrapper delete --request <id> | --user <id> | --value <text>` removes the matching rows, blobs, summaries and vault entries.
- A nightly purge job applies the retention periods.

## Prompt injection and provenance
Untrusted text (documents, tool results, agent output, imported and public logs) can reach components that make decisions. Rules:
- **The planner can never create actions or tool calls.** Actions come only from policy triggers and policy tools.
- **Provenance is tracked** on every text segment and carried through steps. A step's output inherits the least-trusted source of its inputs.
- **Policy tools and actions above read-only** can't be triggered by a model turn whose input contained untrusted sources, unless approved.
- **Labeler output only creates proposals,** never auto-applied changes. Proposals from `public` data carry a visible tag.
- **Imported text is data,** never instructions to the wrapper.
- **Tests** cover known injection strings in documents, tool results and imported logs (`test_injection.py`).

## Pattern definition
A pattern is the key that groups all learning, approvals and stats.

`pattern = (base, size_bucket, pattern_version)`
- **base:**
  1. `rule:<id>` when a judge rule matched. Exact and the safest.
  2. Otherwise `cluster:<id>` from a periodic clustering job on redacted-text embeddings, with a minimum cluster size. Steps in no cluster get `cluster:none`.
- **size_bucket:** S / M / L / XL by input tokens (bounds in policy), because cost and success depend on size.
- **pattern_version:** bumped on each re-clustering. Old stats are kept under their version; a mapping table carries them over with reduced weight instead of silently remapping.

**Code routes are only approved on `rule:` patterns,** never on clusters. Before v4 (no clustering), non-rule steps use `cluster:none` plus size bucket.

## Quality signal (defining success)
Checks run cheapest first, and each verdict records which check decided it.
1. **Hard checks (free):** handler validator, JSON schema, no error, output not cut off, no unknown placeholders.
2. **Behavior signals (free):** a retry, rephrase or repeated request soon after, from the same user or session, counts as a soft failure. If a caller sends neither `user` nor `session_id` and the conversation-prefix hash isn't usable, behavior signals are off for that caller.
3. **Logged-answer reference:** on replay, compare a candidate's answer with the answer in your logs (agreement score plus sampled review).
4. **Sampled review:** a stronger model rates the answer against the request on about 5% of LLM steps. New or rare patterns are sampled more often. Reviews run in the background, never delay a response, and are capped per day.
5. **User rating (optional):** outweighs the other signals.

**Rules**
- The policy default model is the baseline and always eligible.
- Another candidate counts only after `min_reviewed_samples` on the same pattern, and its success must reach `min_success` **relative to the default** on that pattern. This prevents drifting toward the cheapest model.
- Initial success chances are seeded from imported labels and logged-answer comparisons.
- When code and the model disagree on a step, the disagreement is logged. It isn't counted against the code automatically, because for math and dates the code is usually right.

## Policy language
One YAML file or a folder of files controls behavior. Code provides building blocks, and the policy wires them together. `wrapper init` writes a commented starter policy.

**Design rules**
- **Declarative only:** complex logic lives in named Python handlers.
- **Schema-validated:** invalid policies never load, and errors point to the exact line.
- **Safe conditions:** a small evaluator built on Python's `ast` with a strict node allowlist: fields, comparisons, `and`/`or`/`not`, `matches` (`google-re2`, so it can't hang), `in`, numbers, strings. A missing field evaluates as false. No calls, loops, imports, attributes beyond field paths, or I/O. CEL can replace it later if needed.
- **Secrets by reference:** `${ENV:NAME}` or `${FILE:path}`. The policy never contains keys. `validate` checks they resolve; traces never record resolved values.
- **Comments preserved:** `ruamel.yaml` round-trip mode keeps comments and order when the CLI writes approved changes.
- **Layers:** base → environment → per-route/per-user → learned. The learned layer lives in the DB and can be viewed and reset. Your YAML file is the source of truth, and only approved changes are written into it.
- **Merge semantics:** maps merge, lists replace, and rule lists merge by `id`.
- **Stable IDs:** every rule, route, trigger and action has a required `id`. Stats and `explain` follow the id across edits.
- **Versioned:** every effective policy is hashed and stored. Each decision records the version.
- **Hot reload and multiple workers:** each worker caches the effective policy (including the learned layer) in memory and checks the active version every few seconds. New requests use the new version; running requests finish on their own. Short mismatches between workers are visible in decisions and harmless.
- **Validation also checks:** named handlers exist, `wrapper/auto` is never a target model, webhook URLs are on the allowlist, and secret references resolve.

**What the policy controls**
| Section | Controls |
|---|---|
| `models` | Real models behind `wrapper/auto`; planner, classifier, reviewer, fallback |
| `privacy` | Patterns, allow-patterns, custom patterns, `trusted` (models and agents), `key_scope`, `response_values`, `non_text`, code-block handling |
| `planning` | Planner choice, plan shapes allowed, pre-check thresholds |
| `patterns` | Size bucket bounds, minimum cluster size |
| `judge` | Tier order, thresholds, rules |
| `routes` | Agents and when they apply; per-route objective and `allow_lossy` |
| `size` | Margin, strategies, chunk size, reroute models, continuation retries |
| `cost` | Objective, price per second, minimum success, profiles, exploration, default-model sampling floor |
| `quality` | Validators, review sample rate, daily review cap, minimum samples |
| `hooks` | Request, step-response and final-response hooks |
| `actions` | Triggers, scope, actions, risk, approvals, URL allowlist |
| `failures` | Fallback and partial-result behavior per step type |
| `limits` | Cost, tokens, time and actions per request; per-call timeouts |
| `retention` | Period per table |
| `learning` | What may change automatically and what needs approval; weight per data source |
| `import` | Field mappings, sample sizes and filters for log files and datasets |

**Example**
```yaml
version: 1
models:
  default: anthropic/claude-sonnet-5-5
  cheap: anthropic/claude-haiku-5-5
  reviewer: anthropic/claude-opus-5-5
  large_context: [anthropic/claude-opus-5-5]

privacy:
  trusted: [ollama/llama-local]     # models and agents that may see real values
  key_scope: conversation           # conversation | global
  response_values: input_only       # caller only gets private values it sent
  non_text: trusted_only            # pass | block | trusted_only
  allow_patterns: ['ORD-\d+']       # never redacted

judge:
  order: [rules, similarity, classifier, default]
  rules:
    - id: calc
      when: step.text matches '^[\d\s+\-*/().]+$'
      use: code.calculator
    - id: sql
      when: step.text matches '(?i)\b(sql|database)\b'
      use: agent.sql

routes:
  - id: default
    allow_lossy: false              # trim/summarize need explicit permission

cost:
  objective: balanced               # cheapest | fastest | balanced
  price_per_second: 0.002           # value of 1 second of waiting, in $
  min_success: 0.85                 # relative to the default model on the same pattern
  exploration: { rate: 0.02, daily_cap_usd: 1.00, mode: background, default_floor: 0.01 }

quality:
  review_rate: 0.05
  min_reviewed_samples: 30          # before a cheaper model can be chosen
  daily_review_cap_usd: 2.00

limits:
  per_request: { usd: 0.50, seconds: 60 }
  per_call_timeout_s: 30

retention:
  requests: 90d
  blobs: 30d
  vault: 90d

actions:
  - id: open_ticket
    scope: final
    trigger: response.json.status == 'escalate'
    do: webhook.ticketing
    risk: external                  # needs approval
    headers: { Authorization: '${ENV:TICKETING_TOKEN}' }

learning:
  auto_apply: [lower_confidence, move_to_llm]
  needs_approval: [new_rule, move_to_code]
  source_weight: { own_live: 1.0, own_import: 0.8, public: 0.3 }
```

**Tooling**
- `init`: writes a starter policy with comments explaining each section.
- `validate`: checks the schema and every expression, and confirms named handlers, allowlisted URLs and secret references.
- `test`: runs fixtures or saved traces against a policy in dry-run.
- `replay`: runs stored or imported traces (dry or live-sample) and produces the savings and accuracy report.
- `diff`: compares two versions, as text and in the decisions they produce.
- `explain`: shows which rule, tier, pattern and cost estimate made each decision.
- `delete`: removes data by request, user or value.
- `health`: checks DB, writer queue, policy version and provider reachability.

## Data import and replay (v1.5)
**Sources**
| Source | Used for | Limits |
|---|---|---|
| Your JSONL log files | Everything: request mix, sizes, tools, private data patterns, logged answers as quality reference, replay benchmark | Depends on which fields they contain |
| Public chat traces (e.g. LMSYS-Chat-1M, WildChat) | Planner and judge patterns, code-able steps, sizes, similarity seeds | Their models and costs aren't yours; sampled, not imported whole |
| Rated datasets (e.g. UltraFeedback, HelpSteer, Arena votes) | Early quality signal: where cheap models are good enough | Not your domain |
| Tool-use benchmarks (e.g. BFCL, ToolBench) | Routing and caller-tools tests | Not normal chat |

Check each public dataset's license and terms before use.

**Importing your JSONL files**
- Main format is JSONL (one record per line), read line by line so large files never load fully into memory. Plain `.json` files are also accepted.
- Malformed lines are skipped and logged with file and line number, so one bad line never stops an import.
- Imports can resume: progress is stored per file, so re-running continues where it stopped.
- Detects common layouts automatically: OpenAI chat, LiteLLM logs, and Langfuse/LangSmith exports.
- For other layouts, a field mapping in the policy's `import` section says where each field lives:
  ```yaml
  import:
    my_logs:
      path: logs/*.jsonl
      messages: $.request.messages
      response: $.response.choices[0].message.content
      model: $.request.model
      tokens_in: $.response.usage.prompt_tokens
      tokens_out: $.response.usage.completion_tokens
      latency_ms: $.duration_ms
      timestamp: $.created_at
      user: $.request.user
  ```
- Missing fields are allowed. Records without cost or latency still help the planner and judge, but not the cost estimates.
- Every record gets `source` (`own_live`, `own_import`, `public`) and an import batch id. Re-imports are de-duplicated by content hash.

**Public datasets:** importers take a sample size, a language filter and a deduplication step. Batch settings are stored in `import_batches`.

**Labeling run**
A strong model labels a sample of imported steps with: split or not, code-able or not, needs real values or not, and answer quality. Labels seed the similarity tier and the success estimates, and create judge-rule **proposals** (tagged by source). The labeling cost is capped and its prompts go through the egress guard and size checks.

**Replay modes**
- **Dry:** decisions, cost estimates, size behavior, redactions and splits, with no model calls.
- **Live sample:** runs a capped sample through the new decisions and compares results with the logged answers, plus sampled review.

The report shows expected savings, steps routed to code, size problems, privacy hits, redaction false positives, split errors, pre-check misses and decisions that look wrong. It becomes the benchmark every later version must beat (see Acceptance criteria).

**Learning rules for imported data**
- Public data can propose patterns and candidates, but can't confirm a cost estimate or a move to code.
- Your imported data counts slightly less than live data, because prices and models may have changed since it was logged.
- Money always comes from current LiteLLM prices. Imported records contribute token counts, latency and quality.

## Message size handling
Every outbound model call goes through `context/`, including the planner, classifier, reviewer, step, combine and labeling calls.

**Budget**
- Model limits come from LiteLLM's model info, and the policy can override them.
- Tokens are counted with `litellm.token_counter` on the final outbound payload, after hooks and egress redaction.
- A correction factor per model is kept by comparing predicted counts with the provider's returned `usage`. The margin rises automatically when predictions are often low.
- Available input = context window − reserved output − overhead − margin (about 10%, adjusted per model).

**Strategies** are ranked by the optimizer for each input, using the default order as a tie-breaker:
| # | Strategy | Lossy | Used for |
|---|---|---|---|
| 1 | Offload to code | no | Large tables, logs, JSON: process locally and send the result |
| 2 | Trim | yes | Drop old turns and noise; keep the system prompt, latest request and pinned facts |
| 3 | Retrieve | yes | Send only the chunks relevant to this step |
| 4 | Summarize | yes | Compress history with a cheap model and cache the summary |
| 5 | Map-reduce | no | Run per chunk, then merge; the merge is size-checked and becomes a tree if needed |
| 6 | Reroute | no | Use a larger-context model |
| 7 | Fail clearly | no | Say what was too big and by how much; never cut silently |

Lossy strategies run only where the route sets `allow_lossy: true` (default `false`). Otherwise the chain skips them and ends in "fail clearly". The strategy used is reported in response metadata.

**Output**
- Work expected to exceed the output limit is split up front.
- Cut-off plain text, and JSON the caller didn't constrain, is continued with the last part as context, up to a set number of retries.
- Cut-off tool calls and caller-constrained JSON are retried with a larger `max_tokens` or a larger model, never continued.
- Long JSON is requested in parts and validated after joining.

**Other limits:** rate limits are handled with LiteLLM's retries and backoff. A context-length error from the provider triggers a re-measure, a raise of that model's correction factor, and the next strategy.

## Cost optimization
The judge says which tools *can* handle a step. The optimizer says which *should*.

**Candidates:** (tool, size strategy) pairs. Tools include code handlers, agents, each configured model, and the classifier call itself.

**Plan shapes compared:** pass-through, split, map-reduce, large model — only after the pre-check says planning may pay off. Choices inside each shape are made greedily per step, which keeps the comparison small.

**Expected cost**
| Part | Source |
|---|---|
| Money | Predicted tokens × current LiteLLM price; declared price per call for agents and APIs |
| Time | Predicted from tokens and past speed; past run time for code at similar input size |
| Failure | (1 − success chance) × cost of the fallback (one level only) |

`expected cost = money + price_per_second × time + failure cost`

**Choosing**
1. Remove candidates that are unsafe (privacy, risk level), unapproved (code for an unapproved pattern), below `min_success` relative to the default, short of `min_reviewed_samples`, or over the remaining budget. The default model is never removed by the success filters.
2. Pick the lowest expected cost.
3. If nothing is left (the default is over budget or unsafe), use the policy fallback or fail clearly.

**Budget:** the request's `limits` are shared across steps. Each step gets what is left.

**Objective:** `cheapest`, `fastest` or `balanced`. It can be overridden per route, or per request through LiteLLM `metadata`.

**Estimates**
- Start from declared profiles and LiteLLM prices.
- Then learned token counts, time and success from `pattern_stats`, weighted by source and recency, per exact model version.
- Every decision stores predicted vs. actual for calibration.
- Exploration runs an alternative candidate in the background and never puts its output in the user's answer. It is capped per day.
- On patterns the cheap model has won, exploration always samples the default model at a minimum rate (`default_floor`), even near the cap, so drift stays visible.

**Safety comes first:** privacy, provenance, risk levels, approvals and quality minimums always apply before cost.

## How the judge decides
The judge returns eligible candidates with a success chance for each. The optimizer picks among them.

| Tier | Method | Adds candidates when |
|---|---|---|
| 1. Rules | Policy rules by `id` | A rule matches; a rule can force a single tool |
| 2. Similarity | Nearest past steps (redacted text embeddings) and their outcomes | Enough past cases above the success threshold |
| 3. Classifier | Small model labels the step code / agent / llm | Tiers 1–2 found nothing confident; skipped when not worth its cost |
| 4. Default | Policy default model | Always present as the fallback candidate |

Code candidates only come from handlers approved for a `rule:` pattern. Tiers 2 and 3 can't introduce new code routes; they can only propose them.

## Planning
- **Rule planner splitting** on `;`, "then" and lists, with safeguards: never split inside code blocks or quotes, treat "if … then" as one step, require a minimum step length, and fall back to no split when unsure. Replay reports the split error rate.
- **Plan graph:** each step has `depends_on`. The scheduler runs steps in order for now; parallel execution later is a scheduler change only.
- **Context per step:** caller system prompt first, then the wrapper's marked step instruction, then the original request and relevant history.
- **LLM planner (v4):** returns a JSON plan from a cheap model; falls back to the rule planner. Its output schema has no field for actions or tool calls.

## Learning loop
- **Inputs:** outcomes and quality verdicts from live traffic, plus imported and labeled data, weighted by source.
- **Keys:** pattern (with version) + tool + exact model version as returned by the provider. A new model version starts from a weakened copy of the previous version's stats.
- **Updates go to the learned policy layer:** confidence per pattern and tool, cost estimates, size strategy order per input type, pre-check thresholds.
- **Moving a step toward the model** (lower confidence, demote a rule) is applied automatically.
- **Moving a step to code:** repeated agreement checks make a `rule:` pattern eligible, a proposal is created, and you approve it. Learning never writes code. It only routes to existing handlers.
- **Runs outside requests,** as scheduled jobs.

## Hooks
Hooks change data in-process. They never have outside side effects.
- **Request hooks:** before planning, on placeholders.
- **Step-response hooks:** on each step result, on placeholders.
- **Final-response hooks:** on the combined answer, before restore. This is where "append text", "redact more" and "block the response" live.

## Response actions (v5)
Actions have side effects outside the request.
- **Triggers:** pattern, structured field, policy tool call, classifier label, or metric. Each trigger has a `scope`: `step`, `final` or `both`. The planner can't create triggers or actions.
- **Action types:** webhook or API, notification, agent hand-off, code handler, DB write, retry/re-plan/escalate.
- **Risk levels:**
  - Read-only actions run automatically.
  - Reversible actions run automatically, but only if they define an undo and their input was trusted. Actions without an undo count as irreversible.
  - Irreversible or external actions need approval unless the policy allows them.
  - Any action above read-only whose triggering turn had untrusted input needs approval.
- **Approvals:** a blocking action that needs approval doesn't hold up the response. The response says it is pending, and the action runs from the queue once approved.
- **Execution:** blocking or queued. Queued actions are stored in `action_queue` and run by the action worker in the shared task queue.
  - Each action has an idempotency key, and its arguments are checked against a schema.
  - Only registered actions run, and webhook URLs must be on the allowlist.
  - A per-request limit stops runaway chains.
  - Dry-run mode logs actions without running them.
- **Privacy:** local actions get real values; outbound actions go through the egress guard.
- **Learning:** rejected or undone actions lower their trigger's confidence.

## Reliability and operations
- **Async core:** the pipeline, executors and storage writer are async. The sync `completion()` runs the async path in its own loop and is used only outside the proxy.
- **One background task queue** for the DB writer, reviewer, exploration and action worker. It is flushed on shutdown.
- **Internal calls:** a dedicated LiteLLM `Router` with proxy callbacks, spend tracking and budgets off, tagged `metadata.wrapper_internal: true`. Spend is reported once, on the outer `wrapper/auto` call. A price for `wrapper/auto` is registered so LiteLLM cost tracking works.
- **Self-call guard:** `validate` rejects `wrapper/auto` as a target model, and a runtime call-depth check catches the rest.
- **SQLite:** WAL mode with one background writer, so requests never wait on DB writes. Learning runs as separate jobs.
- **Streaming:** only the final call streams. A small buffer holds output until any placeholder is complete, then restores and sends it. Combined, hook-modified or action-blocked answers are sent whole.
- **Usage reporting:** `usage` totals all internal calls, and metadata breaks down the cost by step and by overhead.
- **Timeouts:** per internal call and per request (`limits`); the caller's `timeout` becomes the request limit.
- **Failures:** one fallback to the default model, then the policy's `failures` behavior.
- **Health:** `wrapper health` command (and an endpoint when running as a service).
- **Metrics:** fast-path overhead, pass-through rate, pre-check misses, decisions by tier, blocked leaks, redactions by pattern, size strategies used, queue depth, review and exploration spend.

## Database (SQLite) tables
| Table | Holds |
|---|---|
| `requests` | id, time, source, user, session id, conversation hash, input, final output, policy version, fast path, total cost, cost breakdown |
| `spans` | trace tree: id, parent, name, timing, status, attributes (JSON) |
| `steps` | request id, depends_on, text, redacted text, pattern key, pattern version, size bucket, provenance, embedding (of redacted text), needs real values |
| `decisions` | step id, tier, rule id, candidates (JSON), chosen tool, size strategy, predicted cost/time, confidence, reason, policy version |
| `outcomes` | step id, exact model version, actual cost/time/tokens, predicted vs. actual tokens, error, verdict, deciding check, rating |
| `reviews` | step id, reviewer model, score, notes, cost |
| `labels` | step id, label set (split, code-able, needs real values, quality), labeler, batch |
| `pattern_stats` | pattern, pattern version, tool, exact model version, source, runs, success rate, reviewed samples, avg/p90 tokens, avg/p90 time |
| `pattern_map` | old pattern version/id → new, weight (after re-clustering) |
| `vault` | placeholder, value, type, origin, key scope, key version, created at |
| `redactions` | span id, pattern id, placeholder, flagged false positive |
| `token_calibration` | model, correction factor, samples, updated at |
| `rules` | id, definition, status (active, shadow, retired), origin |
| `policy_versions` | hash, effective YAML, layers, loaded at, parent |
| `policy_learned` | the learned layer: key, value, evidence, updated at |
| `policy_proposals` | diff, evidence, source tag, status, decided by |
| `egress_events` | span id, target, trusted, placeholders sent, leak blocked |
| `size_events` | span id, model, tokens, limit, strategy, lossy, chunks, success |
| `summaries` | cached summaries by content hash |
| `blobs` | large payloads by content hash |
| `actions` | id, request/step, trigger id, action id, args, risk, input provenance, status, idempotency key, result, error, undo info |
| `action_queue` | action id, run after, attempts, status |
| `approvals` | action id, requested, decided by, decision, reason |
| `import_batches` | id, source, path, mapping, sample size, filters, record count, imported at |

## Milestones
**v1: Safe pass-through**
1. Package skeleton, types, async core, SQLite storage with background writer and task queue, tracer
2. Policy schema, loader, layers, `expr.py`, secrets, `validate` (including the self-call check), `init` with starter policy
3. Egress guard, vault with origins, keyed placeholders (8+ chars, key scope), Luhn/phone-context/code-block/allow-pattern rules, tolerant restore (text and tool arguments), tool schema redaction, non-text handling, pre-send leak check, `response_values`. Unit-tested on fixtures; no real data is sent before this is done
4. LiteLLM provider pass-through with caller params and tools untouched, internal Router, usage reporting, spend-once test in proxy mode, concurrency test
5. Size budget with correction factors, fail clearly, output continuation (text only), tool-call retry
6. Streaming with placeholder-safe buffering
7. Retention, `delete` command, nightly purge; timeouts, `health`, metrics

**v1.5: Import + replay**
8. Streaming JSONL importer with auto-detect, field mapping, bad-line logging and resume; OpenAI and LiteLLM layouts
9. Public dataset adapters with sampling, language filter and dedup (after license check)
10. Labeling run (proposals only)
11. Replay runner (dry and live-sample) and benchmark report; acceptance baselines recorded

**v2: Rules**
12. Pre-check, safe rule planner, plan graph, step context with caller system prompt, param propagation, pipeline, format-keeping combine, pass-through as a candidate
13. Code handler registry, agent routing, hooks (three stages)
14. Pattern keys (rule id + size bucket), judge tier 1 and default; optimizer v1 with declared costs, default always eligible, budget sharing and failure handling
15. Policy tooling: test, diff, explain, replay commands

**v3: Quality signal**
16. Identity, validators, behavior signals, outcomes
17. Logged-answer comparison, sampled reviewer with caps; verdicts
18. Optional local name detection

**v4: Learning**
19. Pattern stats per exact model version, learned policy layer, learning jobs
20. Clustering and pattern versions, similarity tier, then classifier tier
21. Optimizer v2: learned estimates, calibration, plan shapes, background exploration with default-model floor
22. Proposals and approval flow; LLM planner; full size strategies

**v5: Actions**
23. Registry, triggers with scope, provenance checks, read-only actions, dry-run
24. Approvals, queue and worker, idempotency, undo

## Later
- Parallel execution of independent steps (scheduler change on the existing graph)
- Caching of code results and model calls
- Export traces to Langfuse or Phoenix through OpenTelemetry
- Postgres for multiple users or servers
- Admin view for decisions, proposals, approvals and the learned layer
- CEL in place of the custom evaluator, if needed

## Open questions
- A few sample lines from your JSONL logs (private values can be masked), to write the field mapping and check which fields exist (cost, latency, tool calls, user/session ids, answers)
- Which real models sit behind `wrapper/auto`; which are cheap, reviewer and trusted?
- Which agents exist on day one, and how are they called?
- Which code handlers matter most?
- Embeddings for similarity and clustering: local model or provider API?
- What extra private data matters in your domain? Any allow-patterns (order ids, internal codes)?
- How large do inputs get? This decides which size strategies come first and the size bucket bounds.
- Money or time by default, and the value of one second of waiting?
- Cost target for v2: is 15% lower than direct the right bar?
- Daily caps for review, labeling and exploration spend?
- Retention periods per table?
- Do callers send `user` or a session id today?
- Which actions first, which may skip approval, and how should approvals reach you?
- Who edits the policy?

## Review changes
Where each REVIEW.md item was applied.

| # | Item | Applied in |
|---|---|---|
| 1 | Pattern undefined | Pattern definition; `patterns/`; `pattern_stats`, `pattern_map` |
| 2 | Double-counted spend | Decisions; Reliability (internal Router); `test_proxy_spend.py`; milestone 4 |
| 3 | Prompt injection | Prompt injection and provenance; flow steps 3, 7, 14; Actions; `test_injection.py` |
| 4 | Lost system prompt | Flow steps 6, 7, 11; Planning; `test_system_prompt.py` |
| 5 | Plan before compare | Flow step 6 (two stages); `planning/precheck.py` |
| 6 | Placeholder collisions/linkability | Privacy → Placeholders; `privacy.key_scope` |
| 7 | Redaction false positives | Privacy → Detection; `redactions` table |
| 8 | Value provenance | Privacy → Vault origin; flow steps 4, 9, 15; `vault` table |
| 9 | Retention/deletion | Privacy → Retention; `retention` section; `delete`; milestone 7 |
| 10 | Sync vs. async | Decisions; Reliability; `tasks.py`; `test_concurrency.py` |
| 11 | Caller params | Caller params table; `params.py` |
| 12 | Identity for signals | Flow step 2; Quality signal; `identity.py`; `requests` columns |
| 13 | Default on cold start | Quality signal rules; Cost optimization → Choosing |
| 14 | Acceptance criteria | Versions → Acceptance criteria |
| 15 | Changed placeholders | Flow step 15; Privacy → Placeholders; hard checks |
| 16 | Non-text content | `privacy.non_text`; flow step 4 |
| 17 | Tool definitions | Egress guard; flow step 9 |
| 18 | Model versions | Learning loop; `outcomes`, `pattern_stats` |
| 19 | Comparison data stops | Cost optimization → Estimates (`default_floor`) |
| 20 | Logged answers | Quality signal step 3; `quality/reference.py`; live-sample replay |
| 21 | Token accuracy | Message size → Budget; `token_calibration` |
| 22 | Lossy strategies | Strategies table; `allow_lossy` |
| 23 | Cut-off tool calls | Message size → Output; flow step 9 |
| 24 | Fragile splitting | Planning |
| 25 | Plan as graph | Planning; `planning/graph.py`; `steps.depends_on` |
| 26 | Expression language | Decisions; Policy design rules (`ast` + RE2) |
| 27 | Secrets | Policy design rules; `policy/secrets.py` |
| 28 | Multiple workers | Policy design rules (hot reload) |
| 29 | Replay quality | Replay modes |
| 30 | Public dataset size | Import → Public datasets |
| 31 | Hooks vs. actions | Hooks section; Actions list |
| 32 | Operations | Reliability and operations; milestone 7 |
| 33 | Two `budget.py` | `context/token_budget.py`, `optimizer/spend_budget.py` |
| 34 | Trusted in two places | `privacy.trusted` only |
| 35 | Final response hooks | Hooks; flow step 12 |
| 36 | Milestone order | Egress is milestone 3, provider is milestone 4 |
| 37 | Starter policy | `wrapper init`; `policy/starter.yaml` |
