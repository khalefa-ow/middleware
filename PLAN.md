# LLM Wrapper: Plan (rev 5, staged)

This is the single plan file for the project. Read it in this order:

| Section | What it is for |
|---|---|
| §1–§2 | The goal and where the project stands today |
| §3 | The evidence so far and the gates that decide what gets built |
| §4–§6 | Scope, architecture as built, and decisions |
| **§7** | **The stages: every open task, in build order, with files and "done when" checks** |
| §8–§10 | Acceptance criteria, risks, open questions |
| **§11** | **The context pruner design** (stages 2 and 4 build it) |
| §12 | Old task ids (M…, PC…, T…) mapped to stage tasks |
| §13 | Working on the project: setup, rules, code map, approvals, progress log |

Other files: `README.md` is the user guide. The old rev-2 design of deferred parts is in git history only
(`docs/PLAN-rev2.md` at commit 22e5128); where it conflicts with this file, this file wins.

---

## 1. Goal
**A platform to optimize and tune LLM usage**, built around one loop:

```
record ──► experiment ──► compare ──► promote ──► (record again)
gateway     replay recorded   cost, latency,   policy route you
traces      calls as variants quality scores   approve (versioned)
```

- **Record:** a LiteLLM gateway (`model="wrapper/auto"`). Callers switch by model name. It records every call with params, usage, cached tokens and cost. It also redacts private data before egress and keeps calls within model limits.
- **Experiment:** replay a sample of recorded calls under variants, called arms. Arms vary the model, params and system prompt. Later they also cover context strategies and fine-tuned models. Every arm call goes through the same gateway pipeline as production.
- **Compare:** cost, latency, cache share, structural validity, similarity, a blind LLM judge, and (best) the caller's own outcome.
- **Promote:** the winner becomes a proposed policy route. A person approves it, and it is versioned and can be rolled back.

The gateway also:
1. optionally **reduces exposure** of detectable private data to model providers (opt-in)
2. **keeps every call within model limits** without silently dropping content
3. **routes** each request by policy (what the loop promotes)

**Success, in business terms.** The platform stays generic: each caller picks its **objective**, and experiments, promotion and reports use it (see §8):
- `cost`: spend at least X% lower than calling the default model directly, at equal measured quality
- `quality`: a higher score or outcome rate, with spend as a guardrail
- `cost_per_success`: spend divided by successful runs (for agent callers such as cppagent, where 26% of runs succeed and caching is already realized)
- when privacy is on: 0 detected private values sent to untrusted providers in the leak suites
- p95 overhead under 30 ms on the pass-through path

**No longer goals of this project:**
- **Response-triggered actions** (webhooks, approvals, undo) become a separate project. They are a workflow engine, not a gateway.
- **Privacy is a nice-to-have, not a requirement** (owner, 2026-11-15). Keep what is built and keep it tested, but make it opt-in in the starter policy and stop investing: no H4 labeling and no local name detection unless a caller requires it.
- **Request decomposition** (split, map-reduce, combine) is conditional on validation gate H3 (§3). The first real caller is an agent with its own planner, where the wrapper's planner would duplicate work.

## 2. Status
The record → experiment → compare loop is built and tested (101 offline tests, `.venv/bin/pytest -q`).
**Promote** exists only as a printed proposal. No gate except overhead is decided, because there is no
production traffic yet.

| Stage | What it delivers | State |
|---|---|---|
| 0 Foundation | Gateway, tracing, replay experiments, run-level A/B harness, cache-aware cost, opt-in fallback | **Built** |
| 1 Measure and tidy | Per-caller objective, privacy opt-in, prompt structure (PC1), context accounting, generic importer | Open, offline, no spend. **Next** |
| 2 Pruner v0 | Cache-safe context pruner with rules and a lexical scorer, as an experiment arm and an opt-in route option | Open, offline, no spend |
| 3 Live evidence | A/B on fastlanes, owner's traces, pruner replay; decide gates H1, H2, H5, H7, H8, then re-plan | Needs the owner's go-ahead (spend) |
| 4 Learned pruner | A small local model that replaces the lexical scorer | Gated on H8; needs new dependencies |
| 5 Promote and operate | Approval flow with versions and rollback, live outcomes, proxy mode, fail-open, response cache | Open |
| 6 Learning and conditional work | Learned routing, section-level arms, fine-tuning, decomposition | Each gated (§3) |

Live checks done so far (about $0.60 in total):
- **M1 live check (2026-11-10):** a live DeepSeek call redacted an email and a phone and restored both; streaming worked. Fastlanes stage 2 through `wrapper/auto`: 10 calls, task passed, token totals equal cppagent's exactly, and the provider's `response_format` rejection reached cppagent unchanged. With n = 1 per arm this is not a parity test.
- **Overhead:** `wrapper bench` on all 533 fastlanes prompts: p50 2.4 ms, p95 5.2 ms, p99 9.6 ms.
- **Caching:** live cache hits of 3,200 of about 3,370 prompt tokens; prefixes stay byte-stable across turns.

## 3. Evidence and validation gates
Every major part rests on an assumption. Each one has a test, a go rule and a fallback. A part
is built only after its gate passes.

### What the fastlanes traces show (35 runs, 533 calls, $0.83 at current prices)
| Finding | Number | Consequence |
|---|---|---|
| Spend is concentrated in repair | repair 67%, coder 18%, evolve 10%, planner 4% | The cost lever is fewer and cheaper repair calls, not planning |
| Runs mostly fail | 26% of runs succeed | Quality per call matters more than price per call |
| Prompts repeat their prefix | 48% of prompt text repeats an earlier prompt in the same run | Provider prompt caching is worth about 13% of spend. The cost model must include it |
| Many calls fail on config | 58 errors: rejected `response_format`, model not found, bad credentials | Error-aware routing and fallback have direct value |
| Structured output is forced | most cppagent calls carry `response_format` | Under current rules these pass through, so routing must work on forced-format calls too |
| Redaction false positives are real | 16-digit query sums passed the Luhn check | Fixed: cards need a prefix plus grouping or a card word. Dry run is now clean |
| The caller plans itself | cppagent has planner, coder and repair roles | Wrapper decomposition would duplicate the caller's planning |

### What public traces show (sampled through the dataset-viewer API, priced at DeepSeek)
Sources: 300 random runs of [nebius/SWE-rebench-openhands-trajectories](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories)
(CC-BY-4.0; OpenHands with Qwen3-Coder, resolved/unresolved labels) and 500 random conversations of
[allenai/WildChat-1M](https://huggingface.co/datasets/allenai/WildChat-1M) (ODC-BY; GPT-3.5/4 chat).

| Finding | Coding agent (300 runs) | Chat (500 conversations) | Consequence |
|---|---|---|---|
| Calls | 19,444 (65 per run) | 1,148 (2.3 per conversation) | |
| Prompt size p50 / p90 / max | 27.9k / 54.7k / 125k tokens | 0.4k / 2.2k / 11k | Agents need size handling. Chats rarely do |
| Prompt : completion tokens | 108 : 1 | about 2.6 : 1 | Agent spend is almost all re-sent input |
| Prompt tokens repeating the previous call | 98% | 82% | |
| Saving from provider prompt caching (cached input at 0.26 × price) | **72% of spend** | 38% | **Caching is the biggest single lever, far above routing or planning** |
| Exact duplicate prompts | 0 | 80 (7%) | A response cache only helps chat-style traffic |
| Task success | 49% resolved | n/a | Outcome labels exist in public agent data |
| Private data in prompts | 1,113 emails (436 in git author/commit lines, the rest the agent's own identity or test addresses), 2 AWS-format keys (fake test fixtures), 1 SSN (Faker) | 10 emails (WildChat is already PII-scrubbed) | Agent traffic carries real personal data through repository content |
| New false positives found and fixed | grep line numbers parsed as an email (`84-@pytest.mark…`), an epoch timestamp after "call:" parsed as a phone, `-+0123456789` parsed as a phone, version strings parsed as IPs | | Regression tests added |

### First experiments (fastlanes, 20 samples per role, judge `gpt-5.3-codex`, total $0.54)
| Role | Arm | Cost vs. control | Valid JSON | Judge vs. logged | Verdict |
|---|---|---|---|---|---|
| planner | `deepseek-chat` re-run (control) | 1.00× | 0.85 | 0.28 | |
| planner | `gpt-6-luna` | 0.44× | 0.70 | 0.15 | Cheaper but worse; not promoted |
| repair | `deepseek-chat` re-run (control) | 1.00× | 0.92 | 0.38 | |
| repair | `gpt-6-luna` | 2.08× | 0.71 | 0.45 | Costlier and 7× slower; not promoted |

What this taught us about the platform:
- **Compare against a control arm, not the logged answer.** Even the same model scores 0.28–0.38 against its own logged answers. Replays of imported traces aren't faithful, because cppagent's traces don't record `temperature` or `response_format`. Gateway-recorded traffic stores params and is replayed with them.
- **20 samples only separates large effects** (the judge's spread is about ±0.2). The promotion bar needs at least 20 judged samples; real decisions need about 100.
- **The best scorer for agent callers is the caller's outcome.** For cppagent that means apply the patch, compile and compare the output, rather than a judge. Per-call outcome scoring isn't possible from traces alone (the project state at the time of the call isn't recorded), so outcomes are measured per run: a run-level A/B harness (M5b).
- **With confidence intervals (added later, same data):** `gpt-6-luna` gives valid JSON less often than the control for both roles (planner −0.15, CI [−0.25, −0.05]; repair −0.21, CI [−0.39, −0.05]). Its judge-score differences are within chance (planner −0.12, repair +0.07).
- **DeepSeek already caches about 90% of these prompts.** For this caller the caching lever is realized as long as the gateway keeps prefixes stable, which is verified.


### What the traces say about context pruning (2026-11-17, no spend)
| Finding | Number | Consequence |
|---|---|---|
| Fastlanes prompts are small | p50 prompt tokens: planner 1.9k, coder 1.5k, repair 2.1k, evolve 8.2k | There is little input to prune for cppagent |
| Fastlanes output is as large as its input | 1.00M completion tokens against 1.14M prompt tokens | For cppagent the cost lever is output and failed runs, not input |
| Fastlanes has no tool messages | 533 calls hold 1,423 user, 517 system and 16 assistant messages, and no tool message | A tool-result pruner has nothing to act on here. Only `evolve` (one user message of about 24k characters) is a candidate, and that is section-level work (PC3) |
| Tool-heavy agents are the target | Public coding agent: p50 27.9k prompt tokens, 108 : 1 prompt to completion, 98% of each prompt repeats the previous call | Almost all spend is re-sent context. But caching already saves 72%, so a pruner that changes earlier messages would lose more than it saves |

So the pruner is for tool-heavy agent traffic (the owner's incoming traces, or the public agent sample), and it
must never change bytes the provider has already cached. Gate H8 decides it on such traffic before any model is trained.

### Gates
| # | Assumption | Test | Go if | Status |
|---|---|---|---|---|
| H1 | There is spend worth saving | `analyze` on real traffic: monthly spend, role mix, cache share | Monthly spend justifies the build at a 15% saving | **Open:** fastlanes is a test bed ($0.83). Production volume is needed |
| H2 | A cheaper model is good enough for a share of calls | Replay 300–500 logged calls on the cheap model; blind strong-model comparison with the logged answer; for cppagent also compile/test outcome | Equal or better on ≥ 40%, predictable from role or features | Open. First data (n = 20 per role): `gpt-6-luna` is not good enough for planner or repair. Needs ~100 samples, more candidate models and run-level outcomes (M5b) |
| H3 | Decomposition pays after prompt caching | Label 200 chat requests; cost planned vs. pass-through with caching | Lower cost on ≥ 10% of traffic at equal quality | **Fails for agent callers** (they plan themselves, and splitting would break a 98% cache prefix). Unlikely for chat (short prompts). Treat as dropped unless a caller shows otherwise |
| H4 | Redaction is accurate and harmless | Hand-label 200 records; detection precision and recall; 100 requests redacted vs. not | ≥ 95% recall on structured PII, < 1% false positives, no significant quality drop | **Parked** (privacy is nice-to-have). Five false-positive classes were found and fixed; recall and quality are not measured. It reopens if a caller requires privacy |
| H5 | Enough volume to learn from | Requests per day per role or pattern | ≥ 50 per day for the top patterns | Open |
| H6 | Overhead is small | Benchmark pass-through with redaction | p95 under 30 ms | **Passed:** p95 5.2 ms on fastlanes prompts |
| H7 | Treating the prompt as code (inferred sub-prompts) finds wins that whole-prompt tuning misses | PC1 on fastlanes and the incoming traces: token share and variability per section; then section arms (drop, shorten, reorder) on the top sections, scored by the caller's objective (replay plus judge, then run-level A/B) | At least one section change per role improves the caller's objective (paired CI clears the bar), for example equal quality at ≥ 15% lower cost **after caching**, or higher quality at equal cost | Open. PC1 needs no spend |
| H8 | Context pruning pays **after** prompt caching | `wrapper prune simulate` on tool-heavy sessions (S2.5), then a live-sample replay with a `prune` arm (S3.5) | Simulated input cost after caching is at least 15% lower with used-unit recall ≥ 0.95 (§11.6), and the replay's primary score is not significantly worse than the control over ≥ 100 samples. Agent callers also need the run-level A/B | Open. Fastlanes cannot decide it (see above) |
| H9 | A learned scorer beats the lexical one | Held-out sessions: tokens kept at equal used-unit recall; then replay | At recall ≥ 0.95 it keeps at least 25% fewer tokens than the lexical scorer, and the replay's primary score is not worse | Open. Only tested if H8 passes |

Fallbacks: if H8 fails, keep the pruner as an opt-in size strategy only (used when a request does not fit) and stop at stage 2.
If H9 fails, ship the lexical scorer and skip stage 4.

## 4. Scope
| In | Conditional (needs gate) | Out (separate project or later) |
|---|---|---|
| Pass-through gateway with privacy, size, tracing | Request decomposition (H3) | Response actions, approvals, undo |
| Policy routing by role, model or request features | Learned routing (H2 + H5) | Admin UI |
| Prompt-cache-aware cost model and exact response cache | ARI-style learned router (H5, then beats counts on replay) | Postgres, multi-tenant |
| Caller outcome feedback (task success) | Context pruning on live routes (H8) | OpenTelemetry export |
| Replay (dry and live) and acceptance gates | Learned pruner model (H9) | Rewriting or summarizing context with an LLM |
| Context pruner as an experiment arm (stage 2) | Section-level prompt arms (H7) | A pruner built into the answering model |

**Build vs. configure:** LiteLLM already has routers, fallbacks, budgets, caching and a
Presidio PII guardrail. Use them where they fit. Build only what they don't do: the
egress guarantee with restore and leak check, policy routing on caller context,
prompt-cache-aware cost, outcome-driven learning, replay, and cache-safe context pruning.

## 5. Architecture (as built)
```
llm_wrapper/
├── __init__.py        register(), proxy_handler
├── provider.py        CustomLLM; sync paths run on one long-lived background loop
├── pipeline.py        identity → route → egress guard → size → call → continue → restore → record
├── policy.py          schema (pydantic, extra=forbid), layers, merge-by-id, ${ENV:}/${FILE:} secrets, self-call check
├── expr.py            ast-allowlist conditions, `matches` (re2 if installed), missing field = false
├── privacy/
│   ├── patterns.py    email, phone (context), card (Luhn + prefix + grouping/context), ssn, api_key, ipv4, custom, allow
│   ├── vault.py       HMAC placeholders, 8→12→16 hex on collision, per-conversation scope, origins
│   └── egress.py      redact messages/tools/tool args/tool results, hint, leak check, tolerant restore, stream restorer
├── size.py            token budget, reroute, fail clearly, continuation rules
├── storage.py         SQLite WAL, background writer, delete/purge, value index
├── importers/
│   ├── cppagent.py    cppagent trace files, with task and run outcomes
│   └── openai_chat.py OpenAI-style chat JSONL (one call per assistant turn, exact cached-prefix tokens)
├── analyze.py         phase-0 report
├── experiments.py     spec, sampling, arms, scorers, judge, bootstrap CIs, report, promotion proposal
├── finetune.py        fine-tune dataset export with a stored train/holdout split
├── ab.py              run-level A/B harness (caller command N times per arm, run_tag, Wilson/bootstrap)
├── cli.py             init / validate / import-cppagent / import-chat / analyze / bench / experiment /
│                      export-finetune / ab / delete / purge / health / show
└── starter_policy.yaml
examples/              experiment specs; the trimmed fastlanes config used for the live check
scripts/               cppagent runner, live smoke and cache probes, public-dataset fetcher
var/fastlanes.db       fastlanes import plus the first experiments' results (local data, not for sharing)
```

### Request flow (v1)
1. LiteLLM calls the provider. The self-call guard refuses depth above `limits.max_depth`.
2. **Identity:** `metadata.session_id`, else a hash of the first system and user message. (LiteLLM does not pass `user` to custom providers, so identity comes from `metadata`.)
3. **Route:** the first policy route whose condition matches, or `metadata.wrapper_route`, or `models.default`.
4. **Egress guard** (untrusted targets only):
   - redact message text, tool-call arguments, tool results and tool schemas
   - add the keep-placeholders hint only when placeholders exist
   - run the leak check, which blocks the call on a hit
5. **Size check** on the redacted payload. If the request doesn't fit: reroute to `large_context`, rerunning the guard if trust changes; otherwise fail clearly with the numbers.
6. **Call** the real model with the caller's params unchanged, tagged `metadata.wrapper_internal`.
7. **Continue** cut-off plain text only when the caller set no `max_tokens`, `tools` or `response_format`.
8. **Restore** text and tool-call arguments (JSON-escaped). Only vault ids are restored, and only origins allowed by `response_values`.
9. **Record** request, spans, egress, size, redactions and vault through the background writer.

Planned additions (not built): `llm_wrapper/prune/` (stage 2, §11), `structure.py` (S1.3), `importers/jsonl.py` (S1.5).
With pruning enabled on a route, step 4 becomes "prune, then egress guard" (§11.4).

## 6. Decisions
Kept from rev 2: async core; egress at the boundary with one restore point; keyed placeholders;
safety before cost; default model always eligible; learned layer separate from the YAML.

New or changed in rev 3:
| Area | Decision | Why |
|---|---|---|
| Trust vs. privacy origin | Two separate fields. **Trust** (for injection) marks tool results, documents and agent output as untrusted. **Privacy origin** counts everything in the request messages as `caller_input`, so `input_only` restores values the caller sent in tool messages | Review rev2 #1; built and tested |
| Caller params | Three lists: *ignored/internal* (`metadata`, `user`, `stream_options`, `api_key`, `extra_headers`, …), *forwarded* (everything else), *force pass-through* (only matters once planning exists). Unknown params are forwarded, not rejected | Review rev2 #3 |
| Continuation | Never when the caller set `max_tokens`: the cut-off is what they asked for. Never for tools or forced formats | Found while building |
| Streaming and response hooks | When final-response hooks exist on a route, that route answers whole instead of streaming. Requests with tools answer whole for now | Review rev2 #2 |
| Sync entry points | One long-lived background event loop, not a new loop per call | Review rev2 #7; built |
| Multiple proxy workers | SQLite with `busy_timeout`; above 4 workers, or for multiple hosts, move to Postgres | Review rev2 #8 |
| Internal model config | Internal calls use LiteLLM with the provider keys from the environment, the same keys the proxy uses. Policy model names must exist in LiteLLM's model map or the proxy `model_list` | Review rev2 #9 |
| Placeholder hint | Appended to the first system message. It is deterministic, so prompt-cache prefixes stay stable across calls in a conversation | Prompt caching (§3) |
| Cost model | `cost = uncached input × price + cached input × cached price + output × price + time + failure`. Cached share is estimated per route from history | Critique #1 |
| Quality signal for agent callers | The caller's own outcome (task success, compile and test results) is the primary label. Callers send it through `metadata.outcome` on the next call, or a `wrapper outcome` API | Fastlanes traces carry task and run success; this is free and strong |
| `min_success` | A ratio to the default model's success on the same route or pattern (0.85 means at least 85% of the default's rate) | Review rev2 #12 |
| Exploration floor | A reserved share of the exploration cap, never above it | Review rev2 #13 |
| Delete by value | Indexed through `vault_refs` (value hash → request ids), written at trace time | Review rev2 #10; built |

Not built yet from this table (each has a stage task): the time and failure terms of the cost model and the per-route
cached-share estimate (S5.7), `metadata.outcome` and `wrapper outcome` (S5.2), `min_success` and the exploration floor
(stage 6). "Streaming and response hooks" is moot while no response hooks exist. The pruner's decisions are in §11.2.

## 7. Stages
Work the stages in order. Inside a stage, tasks are independent unless a task names another one.
Each task lists the files it touches and a "done when" check, which becomes its tests. The rules in §13.2 apply to all of them.

Status legend: ✅ built and tested · 🟡 partly done · ❌ not built · ⏸ parked.

### Stage 0: Foundation (built)
Nothing to do here. This table is the record of what exists and where its evidence is.

| Area | Item | Status | Evidence, or what is missing |
|---|---|---|---|
| Gateway | Provider `wrapper/auto`: sync, async, streaming, one long-lived loop for sync | ✅ | `provider.py`; `test_end_to_end_through_litellm`, `test_async_end_to_end`, `test_stream_restores_across_chunks` |
| Gateway | Identity (`metadata.session_id`, else a hash) and routing (first matching rule, `metadata.wrapper_route`, default) | ✅ | `pipeline.py`; `test_routes`, `test_policy_expr.py` |
| Gateway | Egress guard on messages, tool args, tool results, tool schemas, `response_format`; leak check blocks | ✅ | `test_tools_and_tool_args`, `test_leak_check_blocks`, `test_response_format_schema_is_redacted_for_untrusted_model_only` |
| Gateway | Deterministic hint only when placeholders exist | ✅ | `test_no_hint_without_placeholders`, `test_prefix_byte_stable_across_turns_and_cached_tokens_recorded` |
| Gateway | Size check: reroute to `large_context` or fail clearly; continuation only when uncapped | ✅ | `test_size_fail_clearly_and_reroute`, `test_continuation_only_when_uncapped` |
| Gateway | Restore: vault ids only, origin rules | ✅ | `test_tolerant_restore_only_known_ids`, `test_input_only_masks_other_origins` |
| Gateway | Recording through the background writer; `record_inputs: false`; delete by value; purge; DB 0600 | ✅ | `storage.py`; `test_record_inputs_false_keeps_raw_values_out_of_every_table`; `wrapper delete`, `wrapper purge` |
| Gateway | Health command, call timeouts | ✅ | `wrapper health`; `limits.call_timeout_s` |
| Replay | Experiments: sample, arms, control, spend cap, dry mode, bootstrap CIs, paired comparison, `--limit`, "exploratory" label | ✅ | `experiments.py`, `test_experiments.py` |
| Replay | Scorer plugins and `primary` | ✅ | `test_scorer_spec_validation` |
| Replay | Prompt arms (`system`, `system_file`, `system_suffix`) and the `transform` hook | ✅ | `test_prompt_arm_validation_and_missing_system_is_inserted`, `test_example_transform_keeps_first_and_last` |
| Replay | Run-level A/B harness (`wrapper ab run\|report`) | 🟡 | Built and tested offline (`test_ab.py`). Never run live (S3.1) |
| Replay | Fine-tune export (`export-finetune`, holdout-aware) | 🟡 | `test_finetune.py`. No tuning job; too few examples (stage 6) |
| Replay | Importers: cppagent, OpenAI chat; `analyze` report | ✅ | `importers/`, `test_import_resume_and_analyze` |
| Cost | Cache awareness: markers survive redaction, cached and cache-write tokens priced | ✅ | `test_cache_control_markers_survive_redaction`, `test_cache_write_tokens_recorded_and_priced` |
| Cost | Opt-in error-aware fallback | 🟡 | 6 tests. Repeated fallbacks do not yet become route warnings (S5.6) |
| Cost | Role routing mechanism (`when: metadata.role == …`) | 🟡 | Works. No role route is promoted, and cppagent sends no `role` yet (S3.3) |

### Stage 1: Measure and tidy (offline, no spend, no new dependencies)
**Goal:** finish the offline work that the live stage depends on, and measure where the tokens go.
**Exit:** all six tasks done, suite green, and the S1.3 and S1.4 reports for fastlanes pasted into the progress log.

| Task | What | Files |
|---|---|---|
| S1.1 | Per-caller objective | `experiments.py`, `ab.py`, `README.md` |
| S1.2 | Privacy opt-in in the starter policy | `starter_policy.yaml`, `README.md` |
| S1.3 | Prompt structure inference (PC1): `wrapper structure` | new `structure.py`, `cli.py` |
| S1.4 | Context accounting: `wrapper analyze --context` | `analyze.py`, `cli.py` |
| S1.5 | Generic JSONL importer with field mapping (M6) | new `importers/jsonl.py`, `cli.py` |
| S1.6 | Two counted test suites: prompt injection and param round-trip | `tests/` only |

**S1.1 Per-caller objective.**
- Add `objective: Literal["cost", "quality", "cost_per_success"]` to `Spec` (default `cost`) and to `ABSpec` (default `cost_per_success`). Add `max_cost_ratio: float = 1.25` to `Spec`.
- `promotion_snippet` picks the winner by objective. `cost` is today's rule, unchanged. `quality` picks the arm with the highest primary score whose paired difference to the control is significantly above 0, and whose mean cost is at most `max_cost_ratio` × the control's. `cost_per_success` needs a plugin primary scorer (reject `judge` at load time) and picks the lowest mean cost ÷ mean primary score among arms that are not significantly worse than the control.
- The report prints the objective and its bar in place of the fixed "cheaper than control" text. `ab report` adds one verdict line: the best arm under the objective and whether its difference to the control is significant.
- Done when: existing specs load unchanged and give the same proposal; one test per objective picks the expected arm from a hand-built report; `cost_per_success` with `primary: judge` fails with a clear message.

**S1.2 Privacy opt-in.**
- In `starter_policy.yaml` set `privacy.enabled: false` and say in the comment how to turn it on. The schema default (`Privacy.enabled = True`) and every privacy test stay as they are.
- Done when: a test loads the starter policy, sends an email address to the fake model and finds it unredacted; the same policy with `enabled: true` redacts it; README says privacy is opt-in.

**S1.3 Prompt structure inference (PC1).**
- New command `wrapper structure --source S [--role R] [--json]`. For each role: group system messages by exact text (count, tokens). Split user messages into segments on markdown headers, blank-line blocks and lines that repeat across prompts. Align segments across the role's prompts. Report each segment's label (its header or first 40 characters), share of the role's prompt tokens, how often it is present, and its variability (share of prompts in which its text differs from the most common text).
- Report a confidence per segment: high when its boundary appears in at least 90% of the role's prompts. Segments below that are listed as "unsure".
- Done when: on a synthetic fixture of 30 prompts built from a known template the command recovers the template's segments and marks a constant section as 0% variable; on `var/fastlanes.db` it runs in under 30 s and its output for `evolve` shows the `# Another candidate` and `# Already tried from this parent` sections.

**S1.4 Context accounting.** This is the first input to gate H8.
- `wrapper analyze --context [--source S]` adds a table per role: prompt tokens by message role (system, user, assistant, tool), and for tool and user messages the token share by size bucket (under 400, 400–2k, 2k–8k, over 8k tokens). Per session it reports how many prompt tokens are *first-time* (new in this call) and how many are *re-sent* (already in the previous call of the session), using the existing longest-common-prefix code.
- It ends with one line per role: "prunable upper bound", the share of prompt tokens that sit in tool messages of at least 400 tokens.
- Use the byte estimate (bytes ÷ 4) for tokens, as `size.py` does on the fast path.
- Done when: a fixture session with known message sizes gives the expected table; fastlanes reports 0% in tool messages.

**S1.5 Generic JSONL importer (M6).**
- `wrapper import-jsonl FILE --map messages=PATH --map model=PATH [--map completion=…] [--map prompt_tokens=…] [--map completion_tokens=…] [--map session=…] [--map role=…] [--map outcome=…] [--source NAME]`. A path is dotted (`request.body.messages`). `--preset litellm` fills the map for LiteLLM's log layout.
- Rows without `messages` or `model` are counted and skipped, never guessed. The import can be resumed like the existing importers.
- Done when: a fixture in a made-up layout and one in LiteLLM's layout both import, `analyze` runs on them, and a rerun adds no rows.

**S1.6 Counted suites.**
- Injection suite: at least 10 cases where a tool result or document contains an instruction to reveal a placeholder's value or to repeat a private value; assert no real value reaches `fake.sent`.
- Param round-trip suite: every param outside `_IGNORED_PARAMS` that LiteLLM accepts for chat reaches `fake.calls[0]` unchanged.
- Done when: both suites pass and §8 can cite their counts.

### Stage 2: Pruner v0 (offline, no spend, no new dependencies)
**Goal:** the whole pruner path, measured end to end, before any model is trained. The design is in §11; read it first.
**Exit:** S2.1–S2.6 done; `wrapper prune simulate` runs on a tool-heavy fixture; with pruning off the gateway's bytes and overhead are unchanged.

| Task | What | Files |
|---|---|---|
| S2.1 | Blocks and units: pick prunable blocks, split them into units | new `prune/blocks.py` |
| S2.2 | Rule reducers: exact repeats, repeated lines, terminal noise | new `prune/rules.py` |
| S2.3 | Query and lexical scorer, budgeted selection | new `prune/lexical.py` |
| S2.4 | `prune_messages()`: markers, determinism, report | new `prune/core.py`, `prune/__init__.py` |
| S2.5 | Offline evaluation: usage evidence and `wrapper prune simulate` | new `prune/usage.py`, `prune/simulate.py`, `cli.py` |
| S2.6 | Experiment arm `prune:` and the opt-in live path | `experiments.py`, `policy.py`, `pipeline.py`, `storage.py`, `starter_policy.yaml`, `README.md` |

**S2.1 Blocks and units.**
- `find_blocks(messages, cfg) -> list[Block]`. A block is the text content of one message whose role is in `cfg.roles` (default `["tool"]`) and whose size is at least `cfg.min_block_tokens` (default 400, byte estimate). Never a block: system messages, the last user message, assistant messages, content with non-text parts, content carrying a `cache_control` marker.
- `split_units(text) -> list[Unit]` with character offsets. Units are blank-line-separated paragraphs; a paragraph longer than 40 lines is split every 40 lines; a fenced code block is never split across units unless it exceeds 40 lines. Joining the units' text gives the input back exactly.
- Done when: the round-trip property holds on 200 random texts (plain loop, no new dependency); the protected kinds are never returned as blocks.

**S2.2 Rule reducers.** Each takes and returns units, and records what it removed.
- `drop_exact_repeats`: a block whose text equals an earlier block in the same message list is replaced by a one-line marker naming the earlier block's position.
- `collapse_repeated_lines`: a run of more than 5 identical lines (after stripping digits and timestamps) keeps the first 2 and the last 1.
- `strip_terminal_noise`: ANSI escape codes and progress-bar redraw lines.
- Done when: each reducer has a test with a before and after; a block with nothing to reduce comes back byte-identical.

**S2.3 Query and lexical scorer.**
- `derive_query(messages, block_index) -> str` uses only messages before the block: the first user message (first 1,000 characters), the last user message before the block, and the assistant message that asked for this result: its text plus the name and arguments of the tool call whose `tool_call_id` matches.
- `score_units(query, units) -> list[float]`: BM25 over word and identifier tokens (split on non-alphanumerics and camelCase or snake_case borders), plus a fixed bonus for units that contain an error marker (`error`, `Error`, `FAILED`, `Traceback`, `Exception`, `warning:`, a non-zero `exit code`).
- `select(units, scores, cfg) -> kept indexes`: always keep the first unit and the last two; add units by score until the kept tokens reach `cfg.keep_ratio` (default 0.3) of the block or `cfg.min_keep_tokens` (default 150), whichever is larger; then add the direct neighbours of every unit that got an error bonus. If the query has fewer than 3 distinct tokens, keep everything (abstain).
- Done when: on a fixture test log the failing test's unit is kept and unrelated passing output is dropped; an empty query keeps the block whole; the same input always gives the same output.

**S2.4 `prune_messages(messages, cfg) -> (messages, PruneReport)`.**
- For each block in order: reducers, then scorer and selection, then rebuild the text from kept units in the original order. Each gap becomes one line: `[… N lines omitted by gateway …]`. A block where less than 10% would be removed is left untouched.
- **Prefix purity:** the result for a block depends only on the original messages up to and including that block, and on `cfg`. Nothing after the block, no clock, no randomness.
- The input list is not mutated. `PruneReport` holds per block: message index, tokens before and after, kept unit spans, reducers that fired, scorer name, elapsed ms.
- Done when: (a) appending further messages to a conversation leaves every earlier pruned block byte-identical (the property test for cache safety); (b) the output of `prune_messages` on a growing conversation always has the previous call's output as a prefix; (c) with `cfg.enabled = False` the function returns the same list object.

**S2.5 Offline evaluation.**
- `usage.used_units(messages, block_index) -> set[int]`: a unit counts as *used* if a later assistant message (its text or tool-call arguments) contains one of the unit's distinctive tokens. A token is distinctive if it is at least 6 characters long, has a digit, underscore, dot, slash or inner capital, and appears in at most 3 units of the block. Blocks with no later assistant message are skipped.
- `wrapper prune simulate --source S [--role R] [--keep-ratio X] [--scorer lexical|rules] [--cached-price-ratio 0.1]`: walks each session's calls in time order, prunes every call, and prints per role: blocks touched, prompt tokens before and after, estimated input cost before and after (first-time tokens at the full price, re-sent tokens at the cached price, with the same pricing code as `analyze`), used-unit recall, and prune time p50 and p95. It makes no model call and writes nothing.
- Done when: a fixture session with a known used unit reports recall 1.0 when that unit is kept and less when it is forced out; the cost line matches a hand calculation on the fixture.

**S2.6 Experiment arm and the opt-in live path.**
- Experiments: `Arm.prune: PruneConfig | None`. It is applied after the system changes and before `transform`. The report adds mean prompt tokens per arm.
- Policy: a new top-level `prune:` section (`PruneConfig`: `enabled: false`, `roles`, `min_block_tokens`, `keep_ratio`, `min_keep_tokens`, `scorer: lexical`). Pruning runs only when `prune.enabled` is true **and** the matched route has `allow_lossy: true` (the field exists already).
- Pipeline: in `Wrapper.prepare`, after the route is chosen and before `_guard`, call `prune_messages` inside `try/except`. On any exception send the original messages and record the error. `Prepared.caller_messages` stays the original, so recordings can be replayed with a different pruner.
- Storage: add `requests.prune_json` through `MIGRATIONS` (tokens before and after, blocks, ms, scorer, error). With `record_inputs: false` it holds numbers only.
- An in-process LRU (keyed by a hash of block text and query) avoids re-scoring old blocks on every call of a session.
- Done when: with `prune.enabled: false`, or on a route without `allow_lossy`, `fake.sent` is byte-identical to today's; with it on, a three-turn fixture conversation shows the pruned block identical in all three outbound calls; a scorer that raises still produces a normal answer and a recorded `error`; `wrapper bench` with pruning off shows no change, and with it on adds at most 15 ms p95 on a 30k-token prompt after the first call.

### Stage 3: Live evidence (needs the owner's go-ahead, see §13.4)
**Goal:** replace assumptions with measurements, then re-plan.
**Exit:** gates H1, H2, H5, H7 and H8 each have a recorded decision in §3, and the plan for stages 4 to 6 is confirmed or changed.

| Task | What | Needs | Rough cost |
|---|---|---|---|
| S3.1 | Run the fastlanes A/B (`examples/cppagent_ab/`), 5 runs per arm | Go-ahead, and a decision on the starting state: empty folder, or a snapshot after stage 1 (`SEED_DIR`) | $0.50–1.00, 20–40 minutes |
| S3.2 | cppagent sends `metadata={"role", "session_id", "task_id"}` (M4); document the contract in `README.md` for other callers | Go-ahead to edit the owner's other repository | Free |
| S3.3 | Import the owner's traces (S1.5 if the format is new); run `analyze`, `analyze --context`, `structure` and `prune simulate` on them (M7) | The traces | Free |
| S3.4 | Fetch a sample of public coding-agent sessions (`scripts/fetch_hf_rows.py`, nebius/SWE-rebench-openhands-trajectories, CC-BY-4.0) into `var/`, and run `analyze --context` and `prune simulate` on it | Go-ahead for the download | Free |
| S3.5 | Pruner replay: an experiment with a control arm and a `prune` arm on about 100 tool-heavy samples, judged | S3.3 or S3.4, and go-ahead. Quote the `--dry` estimate when asking | From the dry run |
| S3.6 | Strong-arm experiment on planner and repair, about 100 samples, then `export-finetune --select experiment:ID:ARM` | Go-ahead | About $3 |
| S3.7 | Section arms by hand: for the top two sections S1.3 finds, write `transform` arms that drop or shorten them, and replay | Go-ahead. Quote the `--dry` estimate | From the dry run |
| S3.8 | Decide the gates and re-plan: write each decision into §3 and update §2 and §7 | S3.1–S3.7 | Free |

### Stage 4: Learned pruner (only if H8 passes; needs go-ahead for new dependencies)
**Goal:** replace the lexical scorer with a small local model trained on our own traffic, and ship it only if it beats the lexical scorer (H9).
The design is in §11.7. The tasks get full "done when" checks when the stage opens, because they depend on what stage 3 shows.

| Task | What |
|---|---|
| S4.1 | Training set from recorded sessions: (query, block, unit labels). Labels from usage evidence (free), split by session into train and holdout |
| S4.2 | Teacher labels on a sample: a strong model sees the query, the block and what the agent did next, and quotes the units it needed. Measure agreement with usage evidence. Needs go-ahead (spend) |
| S4.3 | Train a unit classifier on an encoder with an 8k window. Optional install extra `llm-wrapper[prune]` (`torch`, `transformers`); the gateway itself gains no dependency. This machine has two A40 GPUs |
| S4.4 | Serving: `wrapper prune-serve` (a local sidecar process) and a client scorer `scorer: model` with a timeout; on timeout or error the block is sent unpruned, and that decision is stored so later calls repeat it |
| S4.5 | Evaluate against the lexical scorer on held-out sessions and in replay (H9). Optional: chopper as one more comparison arm through the `transform` hook, in the evaluation environment only |

### Stage 5: Promote and operate
**Goal:** a winning arm can become a live route safely, and the gateway is fit to run for other callers.
**Exit:** one route has been proposed, approved, applied and rolled back in a test; outcomes arrive live.
S5.1 and S5.2 do not depend on stage 4 and can start as soon as stage 3 has produced a winner.

| Task | What | Old id |
|---|---|---|
| S5.1 | Approval flow: `wrapper propose` stores a proposal (experiment, arm, evidence, policy diff); `approve` writes a new policy version; `rollback` restores the previous one; every request records the policy hash (it already does) | M18 |
| S5.2 | Live outcomes: accept `metadata.outcome` for the previous call of a session and a `wrapper outcome SESSION RESULT` command; gateway validators (JSON parse, schema) recorded per request | M12 |
| S5.3 | Proxy mode: run under the LiteLLM proxy, register a price for `wrapper/auto`, test that spend is counted once. New dependency `litellm[proxy]`: needs go-ahead | M3 |
| S5.4 | Fail-open per route: `on_wrapper_error: direct` skips routing and pruning on an internal error, but never skips privacy | §9 |
| S5.5 | Exact response cache, keyed by redacted payload and model, only for `temperature == 0` or callers that opt in | M9 |
| S5.6 | Repeated fallbacks become route warnings in `wrapper health` and `analyze` | M10 |
| S5.7 | Cost model: add the time and failure terms and a per-route cached-share estimate | §6 |
| S5.8 | Sampled background review by a stronger model with a daily cap, only where no caller outcome exists | M13 |
| S5.9 | Remove the "Task was destroyed but it is pending" warning at process exit after sync calls (needs one real network call to reproduce) | — |

### Stage 6: Learning and conditional work (each item has its own gate)
| Item | Gate | What |
|---|---|---|
| Learned routing | H2 and H5 | M15 pooled stats per route and exact model version; M16 an ARI-style local scorer that ships only if it beats pooled counts on held-out replay; M17 exploration with a default-model floor inside the cap |
| Section-level arms (PC3) | H7 | Arms address an inferred section by name (`drop:`, `replace:`, `move:`) and compile to a transform. Arms touch only high-confidence segments. PC2 (callers declare their sections) stays optional |
| Fine-tuned or distilled model | About 100 successful examples per role (fastlanes has 13–15) | Submit a tuning job and run the holdout experiment. Needs go-ahead (uploads data, costs money) |
| Request decomposition | H3 | Fails for agent callers and is unlikely for chat. Dropped unless a caller shows otherwise. Design in git history (`docs/PLAN-rev2.md`, commit 22e5128) |
| Local name detection | H4 | ⏸ Parked with privacy. Reopens only if a caller requires privacy |
| SQLCipher option | A caller needs encryption at rest | Not built |

**Background for H7, "prompt as code".** Today a prompt reaches the platform as one flat string, so the platform can only
tune it as a whole. Fastlanes prompts are in fact assembled from parts inside cppagent (each role has 1–7 system
prompts, and user messages are made of repeated sections). The owner decided on 2026-11-15 that the platform infers this
structure and callers do not change. If S1.3 recovers the parts, each part becomes a unit to measure: ablate it, shorten
it, reorder it stable-first for a longer cached prefix, and version it as a diff. Limits: inferred structure can be wrong,
changing an early part invalidates the cache for everything after it, and a gain measured by replay still needs the
run-level A/B before promotion.

## 8. Acceptance criteria
| Metric | Target | Status |
|---|---|---|
| Leaks (privacy on) | 0 in leak, injection and non-text suites | 🟡 Leak and non-text tests pass. The injection suite is S1.6 |
| Caller params | 100% forwarded unchanged on pass-through (except redaction and the hint) | 🟡 Covered by pipeline tests. The counted suite is S1.6 |
| Live parity | Same task success as direct; tokens within 5% | 🟡 Tokens matched exactly at n = 1. Success parity needs the A/B (S3.1) |
| Overhead | p95 under 30 ms added on the pass-through path | ✅ 5.2 ms |
| Proxy spend | Counted exactly once | ❌ S5.3 |
| Restore | 100% of vault placeholders restored | 🟡 Restore tests pass; no separate counted suite |
| Redaction false positives | Under 1% on a labeled sample | ⏸ Parked with H4 |
| Objective | The caller's chosen objective improves on live-sample replay (not dry estimates) and, for agent callers, on the run-level A/B. Default target for `cost`: at least 15% lower than direct at equal quality | ❌ No promotion has passed yet |
| Quality | Not significantly worse than direct (paired test, p < 0.05), or equal caller outcome rate | ✅ The mechanism exists (paired CI and promotion bar); no route promoted under it |
| Pruner cache safety | A pruned block is byte-identical in every later call of its session | ❌ S2.4, S2.6 |
| Pruner failure | A pruner error or timeout never fails or blocks a call | ❌ S2.6, S4.4 |
| Pruner value | H8, and for the model H9 | ❌ Stage 3, stage 4 |
| Learned router | Lower cost than pooled counts at equal quality on held-out replay | ❌ Stage 6 |

## 9. Risks
| Risk | Mitigation | Built? |
|---|---|---|
| The wrapper is a single point of failure for every LLM call | Fail-open per route that skips routing and pruning but never privacy. Health command; timeouts | Health and timeouts yes. Fail-open is S5.4 |
| Redaction lowers answer quality | Trusted local model for sensitive routes. Code blocks skipped by default. Privacy is opt-in (S1.2) | Code-block skip yes. Quality effect unmeasured (H4 parked) |
| Regex misses names and free text | Stated plainly: the goal is reduced exposure, not a guarantee | — |
| The local DB concentrates real data | 0600 file, retention and purge, delete by value, `record_inputs: false`; SQLCipher if a caller needs it | All but SQLCipher |
| Pruning removes something the model needed | Recall-first selection, visible markers, head, tail and error context always kept, abstain on weak queries, opt-in per route, promoted only through an experiment and (for agents) the run-level A/B | Stage 2 |
| Pruning breaks the provider's prompt cache | Prefix purity (§11.2), tested as a property; `prune simulate` prices cached and first-time tokens separately | Stage 2 |
| Usage evidence is a weak label (the model may use a fact without quoting it) | Treat it as a lower bound; check it against teacher labels on a sample (S4.2); the final test is always the caller's outcome | Stage 4 |
| Learning never converges at low volume | Gate H5; pooled estimates; the default model stays the baseline | Stage 6 |
| Cheap model drift | Default-model floor in exploration; stats per exact model version | Stage 6 |
| Inferred prompt structure is wrong | S1.3 reports confidence; arms touch only high-confidence segments; callers can declare structure (PC2) | S1.3 |

## 10. Open questions
1. Production traffic: which callers besides cppagent, the monthly spend, and requests per day (H1, H5)? **The owner will supply more traces** (S3.3).
2. Do the incoming traces contain tool-heavy agent sessions? If not, H8 can only be decided on public data (S3.4).
3. Which models count as cheap, default and trusted (local)?
4. What cost target per caller? Is 15% right?
5. Retention periods?
6. For the fastlanes A/B: start each run from an empty folder, or from a snapshot after stage 1 (`SEED_DIR`)?

Answered: the live checks may use the DeepSeek key in cppagent's `.env` (about $0.60 used so far). Privacy is nice to have,
not a requirement (2026-11-15), so H4 is parked and privacy becomes opt-in.

## 11. The context pruner
### 11.1 What it is
A step in the gateway that shortens large tool results before they are sent, keeping exact text and dropping the rest.
It is extractive: it never rewrites or summarizes. It is off by default, opt-in per route, and reaches a live route only
after an experiment shows it helps the caller's objective.

It starts from the idea in [chopper](https://github.com/wiiiktor/chopper_demo) (MIT): a small encoder reads a question and
about 20 retrieved passages in one pass, marks each token keep or drop, and returns exact quotes. We do not use chopper's
code or weights in the gateway. We build our own pruner, because chopper was made for question answering over retrieved
passages and a gateway for agents has different constraints:

| Chopper (as published) | Problem for a gateway | What our pruner does instead |
|---|---|---|
| Stateless: it cuts whatever it is given, each time | Cutting earlier messages differently on a later call changes the prefix and loses the provider's prompt cache, the biggest cost lever in §3 | **Prefix purity:** a block's result depends only on the messages up to that block, so it is the same bytes in every later call (§11.2) |
| Needs an explicit question | An agent call has no single question | The query is derived from the task, the last user message and the tool call that produced the result (§11.3) |
| Marks tokens; whole sentences are restored afterwards with a prose rule | Tool output is logs, code, diffs and JSON; token runs cut through structure | Works on **units** (paragraphs, line groups, code blocks) and never cuts inside one |
| Always cuts to about 2–5% of the text; its own tests show a loss of 6–9 points against reading everything, and it is weaker than XProvence at light trimming | An agent that loses one needed line fails the run | **Recall first:** a keep budget, protected head, tail and error context, and abstaining when the query is weak |
| Dropped text disappears silently | The model cannot tell that something is missing | A visible marker line for every gap |
| Trained on Wikipedia multi-hop questions, business documents and chat; evaluated on answer accuracy | No code, logs or tool output in training; no measure of cost after caching | Labels from **our own traffic** (what the agent used next, §11.6); judged by the caller's objective through the experiment loop |
| A long passage is cut into windows that do not see each other | Tool results can be far above 8k tokens | Windows overlap and each carries the query (stage 4) |
| Published separately from any cost accounting | Savings are not comparable with routing or caching | `prune simulate` prices first-time and cached tokens separately; `requests.prune_json` records every decision |

What we keep from chopper: exact quotes only, one pass over query plus context, a small local encoder rather than an
LLM, and the labelling trick of letting a strong model that can see the outcome mark what was needed.

### 11.2 Decisions
| Decision | Why |
|---|---|
| **Prefix purity.** The pruned form of a block is a pure function of the caller's original messages up to and including that block, plus the config | The caller always re-sends the original history, so the gateway recomputes the same result on every call without stored state. The cached prefix stays byte-identical, and a replay of a recorded call equals what the live path would have sent |
| **Prune on entry only.** A block is pruned the first time it appears and then never changes | A first-time token costs the full input price once and the cached price on every later call, so cutting it on entry saves both. Pruning a block later would rewrite the cache from that point. A deferred mode (prune once a block is N turns old) is not in v0; `prune simulate` can price it later |
| **Extractive, with markers** | Exact text keeps restore and audit simple, and the model sees where text is missing |
| **Lossy means opt-in twice:** `prune.enabled` in the policy and `allow_lossy: true` on the route | Matches the existing rule that the gateway never drops content silently |
| **Prune before the egress guard** | Less text to redact, and the pruner runs locally so it may see real values. Recordings keep the caller's original messages |
| **Fail open** | A pruner error or timeout sends the block unpruned. With a non-deterministic scorer (stage 4) that outcome is stored and repeated, so the prefix still does not change |
| **Tool results only in v0** | They are where the tokens are in agent traffic (§3). Large user messages are section-level work (PC3) |
| **The gateway gains no heavy dependency** | v0 is pure Python. The model runs in a sidecar behind an optional extra |

### 11.3 How a block is pruned (v0)
1. **Find blocks** (S2.1): text of `tool` messages of at least 400 tokens. System messages, the last user message, assistant messages, non-text content and content with `cache_control` are never touched.
2. **Reduce by rule** (S2.2): exact repeats of an earlier block, runs of identical lines, terminal noise.
3. **Derive the query** (S2.3) from messages before the block: the first user message, the last user message, and the assistant turn and tool call that asked for this result.
4. **Split into units and score** (S2.1, S2.3): BM25 on word and identifier tokens, plus a bonus for error markers.
5. **Select** (S2.3): the first unit and the last two always; then by score up to the keep budget; then neighbours of error units. A weak query keeps everything.
6. **Rebuild** (S2.4): kept units in order, one marker line per gap. If less than 10% would go, leave the block alone.

Example, a 300-line test log after `pytest tests/test_io.py`:

```
============================= test session starts ==============================
collected 212 items
[… 180 lines omitted by gateway …]
___________________________ test_read_header_v2 ________________________________
    def test_read_header_v2():
>       assert read_header(buf).version == 2
E       AttributeError: 'NoneType' object has no attribute 'version'
tests/test_io.py:88: AttributeError
[… 95 lines omitted by gateway …]
=========================== 1 failed, 211 passed in 4.12s ======================
```

### 11.4 Where it sits
```
prepare():  identity → route → [prune, if prune.enabled and route.allow_lossy] → egress guard → size check → call
```
- Experiments: `Arm.prune` applies the same function to a replayed call (S2.6).
- Size handling: when a request does not fit and the route allows lossy handling, pruning is tried before "fail clearly".
  This is a later, separate switch (`size.prune_to_fit`), not part of stage 2.
- Streaming, restore and continuation are unaffected: pruning only changes the input.

### 11.5 Configuration
```yaml
prune:
  enabled: false          # opt-in
  roles: [tool]           # message roles whose content may be pruned
  min_block_tokens: 400   # smaller blocks are never touched
  keep_ratio: 0.3         # upper bound on cutting: keep at least this share of a block
  min_keep_tokens: 150
  scorer: lexical         # rules | lexical | model (stage 4)

routes:
  - id: agent-tools
    when: "metadata.role == 'coder'"
    model: deepseek/deepseek-chat
    allow_lossy: true     # pruning only runs on routes that say so
```

### 11.6 How it is measured
| Level | Measure | Costs money? | Task |
|---|---|---|---|
| Unit | **Used-unit recall:** of the units the agent demonstrably used later (it quoted a distinctive token from them), the share the pruner kept. Target ≥ 0.95. It is a lower bound on what was needed, not the truth | No | S2.5 |
| Session | **Input cost after caching:** first-time tokens at full price, re-sent tokens at the cached price, before and after pruning | No | S2.5 |
| Call | **Replay:** control arm against a `prune` arm, scored by the judge or the caller's scorer plugin; paired difference with a CI | Yes | S3.5 |
| Run | **A/B:** the caller's own command under a policy with pruning on, compared on success rate and cost per success | Yes | Stage 3 or 5 |
| Latency | Added p95 at most 15 ms (lexical, warm) on a 30k-token prompt; nothing added when pruning is off | No | S2.6 |

H8 uses the first three. Promotion to a live route needs the fourth for agent callers.

### 11.7 The learned scorer (stage 4, only if H8 passes)
- **Task:** for a query and a block, give each unit a keep probability. It replaces step 4 of §11.3; everything else stays.
- **Model:** an encoder with an 8k-token window and a linear head on each unit's pooled tokens. Candidates to compare: `jhu-clsp/mmBERT-base` (MIT, the base chopper uses) and `answerdotai/ModernBERT-base` (Apache 2.0). Blocks longer than the window are read in overlapping windows, each with the query; a unit takes the score from the window where it sits most centrally.
- **Labels:** usage evidence from recorded sessions (free, S4.1), checked and extended by teacher labels on a sample (S4.2). Sessions are split into train and holdout as a whole, never by call.
- **Threshold:** set on the holdout so that used-unit recall is at least 0.95; the keep ratio is then whatever results. The model abstains (keeps everything) when its best unit score is below a floor.
- **Serving:** a local sidecar (`wrapper prune-serve`) so the gateway needs no `torch`. Only new blocks are scored; old blocks come from the decision cache. Target: 150 ms p95 per new block on one GPU, with a client timeout that falls back to unpruned.
- **Ship rule:** H9. If it fails, the lexical scorer stays.
- **Not planned:** a pruner built into the answering model (chopper's "integrated" variant). It needs control of the model's weights and cache, which a gateway in front of API providers does not have.

## 12. Old ids
Earlier revisions and the git history use these ids. T1–T15 are done; their full specs are in git history (`HANDOFF.md`, commit 22e5128).

| Old id | Now | Old id | Now |
|---|---|---|---|
| M1, M2, M2b | Stage 0 (done) | M12 outcomes | S5.2 |
| M3 proxy mode | S5.3 | M13 sampled review | S5.8 |
| M4 / M5d caller metadata | S3.2 | M14 local names | Stage 6 (parked) |
| M5, M5b, M5c | Stage 0; live A/B is S3.1; fine-tuning is stage 6 | M15–M17 learning | Stage 6 |
| M6 importers | S1.5 | M18 approval flow | S5.1 |
| M7 production analysis | S3.3, S3.8 | PC1 | S1.3 |
| M8 cache awareness | Stage 0 (done) | PC2, PC3 | Stage 6 |
| M9 response cache | S5.5 | Objective, privacy opt-in (2026-11-15 review) | S1.1, S1.2 |
| M10 fallback | Stage 0; warnings are S5.6 | Context pruner | Stages 2 and 4, §11 |
| M11 role routing | Stage 0 mechanism; promotion needs S3.2 and S5.1 | | |

---

## 13. Working on the project
This is the working brief for whoever works on the project next. It is self-contained. If your context is summarized,
re-read §7 (the stage you are in), this section and the progress log before continuing.

### 13.1 Setup and sanity checks

Work in `/mnt/data/agent-workspace/projects/agentmiddlelayer`. The virtualenv is `.venv`
(Python 3.12, managed by `uv`; LiteLLM 1.108.4).

Run these first. If any result differs, stop and report it before changing anything.

```bash
.venv/bin/pytest -q                      # expect: 101 passed (about 1 minute)
.venv/bin/wrapper --db var/fastlanes.db experiment report fastlanes-planner
                                         # expect a table where arm "luna" costs 0.44x the control
.venv/bin/wrapper --db var/fastlanes.db analyze --source own_import | head -20
                                         # expect 533 calls; repair is about 67% of spend
```

`var/fastlanes.db` holds the 533 imported fastlanes calls plus the results of the first two
experiments. Treat it as read-only reference data: copy it before any run that writes
(`cp var/fastlanes.db var/work.db`). It can be rebuilt (without the experiment results) with
`wrapper --db NEW.db import-cppagent /mnt/data/agent-workspace/projects/cppagent-usecases/fastlanes`.

### 13.2 Rules

These apply to every task. They are not suggestions.

**Money and network**
- Offline work makes no call to a real model and downloads nothing. Tests fake the model (§13.3).
- §13.4 lists work that costs money or changes things outside this folder. Don't start any of it
  without the owner's go-ahead, and quote the estimated cost when asking.

**Secrets**
- API keys live in `/mnt/data/agent-workspace/projects/cppagent/.env`. Never print, copy, log
  or store their values. Printing variable names is fine.
- Never write a real key, or a real person's data, into a test, example or doc.

**Other folders**
- `/mnt/data/agent-workspace/projects/cppagent` and `.../cppagent-usecases` belong to the user
  and have uncommitted changes. Read them freely. Never modify, format or delete anything there.

**Code**
- Don't add dependencies without asking. `pyproject.toml` lists `litellm`, `pydantic`, `pyyaml`.
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
  task in §7 says to.
- If the same fix fails twice, stop, write down what you tried and what happened, and ask.

**Docs**
- When a task changes behavior a user can see, update `README.md` in the same task.
- When a task is finished, mark it in its stage in §7 and update the status table in §2 and the
  acceptance table in §8 where they change. Keep the test count in `README.md` and this file correct.
- After each task, append one line to the progress log (§13.6).

**Git**
- The repository is on GitHub: `git@github.com:khalefa-ow/middleware.git`, branch `main`.
  Commit or push only when the owner asks. `var/`, `*.db` and `.env` are git-ignored on purpose.

### 13.3 Code map

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
| `ab.py` | `run_ab()`, `ab_report()`; tables `ab_experiments`, `ab_runs` | Runs the caller's command in its own process group; sets `WRAPPER_POLICY`, `WRAPPER_DB`, `WRAPPER_RUN_TAG` per run |
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

### 13.4 Needs the owner's go-ahead
Offline tasks (stages 1 and 2) need no approval. Everything below does. Quote the estimated cost when asking.

| Work | Task | Why it needs approval | Rough cost |
|---|---|---|---|
| Live A/B on fastlanes, 5 runs per arm | S3.1 | Calls real models. Also needs the starting-state decision (`SEED_DIR`) | $0.50–1.00, 20–40 minutes |
| cppagent sends `metadata={"role", "session_id", "task_id"}` | S3.2 | Edits the owner's other repository, which has uncommitted changes | Free |
| Download a public agent-trace sample | S3.4 | Network download into `var/` | Free |
| Pruner replay, section-arm replay, strong-arm experiment | S3.5, S3.7, S3.6 | Call real models | Dry-run estimate; about $3 for S3.6 |
| Teacher labels for the learned pruner | S4.2 | Calls real models | Estimate when the stage opens |
| `torch` and `transformers` as the optional extra `[prune]`; model download | S4.3 | New dependencies and a download | Free |
| Proxy mode | S5.3 | Needs `litellm[proxy]` and a running server | Free, but a new dependency |
| Exit warning after sync calls | S5.9 | Only reproducible with a real network call | A few cents |
| Submit a fine-tuning job | Stage 6 | Uploads data to a provider and costs money | Provider pricing |

### 13.5 Out of scope
Don't build these without a plan change, even if they look close: request decomposition or planning, response-triggered
actions, an admin UI, Postgres, OpenTelemetry export, learned routing (stage 6), local name detection, summarizing or
rewriting context with an LLM, pruning anything other than tool results before stage 6, and any change to the detector
regexes that isn't forced by a failing test.

### 13.6 Reporting and progress log
After each task, append a line to the log below: date, task id, what changed in one sentence, and the test count. If you had to deviate from a task's wording, say how and why in that line. When a stage is done, report to the owner which tasks are done (and which are not, and why), the `N passed` line, anything that looks wrong but no task covers, and what the next stage needs from §13.4.

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
- 2026-11-11: T14-T15 done. `cache_control` markers verified to survive redaction (no fix needed); `add_hint` appends a
  text part when the system content is a list; cache-write tokens read from `Usage.cache_creation_input_tokens` /
  `prompt_tokens_details.cache_creation_tokens` (checked in the installed LiteLLM 1.108.4), stored in
  `requests.cache_write_tokens` and priced via `cost_per_token(cache_creation_input_tokens=...)`. Opt-in `fallback:`
  (`Fallback` in `policy.py`, `Wrapper._fallback_target`). Deviations: content-policy errors are also excluded (they
  subclass `BadRequestError` and a retry elsewhere would route around a refusal); an unquoted `on:` in YAML is the
  boolean True, so `Fallback` accepts that spelling as the `on` key; if preparing for the fallback fails, the
  original error is raised. `wrapper ab report` on a database without A/B tables gives a clear error. 101 tests pass.
- 2026-11-11: pushed to `git@github.com:khalefa-ow/middleware.git` (`main`).
- 2026-11-15: plan review. Owner decisions: the platform stays generic (objective per caller); more traces will come; privacy is nice-to-have (H4 parked, opt-in planned); prompt structure is inferred by the platform (PC1). Added the "prompt as code" direction and H7. Implementation checklist written (Part II). All plan files combined into this one: `HANDOFF.md`, `docs/PLAN-rev2.md` and `docs/PLAN-STATUS.md` were merged here and removed. 101 tests pass.
- 2026-11-17: plan rewritten as rev 5 (no code change). Work is now ordered in stages 0–6 (§7), replacing the versions list, the Part II checklist and the task list. Added the context pruner: design in §11, tasks in stages 2 and 4, gates H8 and H9. Read-only check of `var/fastlanes.db` for it: prompts are small and hold no tool messages, so the pruner targets tool-heavy agent traffic and fastlanes cannot decide H8. The rev-2 design appendix and the "changes from rev 2" table were removed; the rev-2 design stays in git history (`docs/PLAN-rev2.md`, commit 22e5128). 101 tests pass (not re-run; no code changed).
