# LLM Wrapper: Build Plan (rev 3)

Rev 3 resets the plan around evidence. It applies the rev-2 review (15 items), the fresh-eye
critique (scope, prompt caching, validation first), the ARI reading, and what the prototype
and the fastlanes traces showed. Rev 2 is kept in `docs/PLAN-rev2.md` for the detailed design
of parts that are now deferred.

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
1. **reduces exposure** of detectable private data to model providers, with measured detection rates
2. **keeps every call within model limits** without silently dropping content
3. **routes** each request by policy (what the loop promotes)

**Success, in business terms** (set targets per caller, see §8):
- LLM spend at least X% lower than calling the default model directly, at equal measured quality
- 0 detected private values sent to untrusted providers in the leak suites
- p95 overhead under 30 ms on the pass-through path

**No longer goals of this project:**
- **Response-triggered actions** (webhooks, approvals, undo) become a separate project. They are a workflow engine, not a gateway.
- **Request decomposition** (split, map-reduce, combine) is conditional on validation gate H3 (§3). The first real caller is an agent with its own planner, where the wrapper's planner would duplicate work.

## 2. Status
| Part | State |
|---|---|
| v1 prototype | Built in `llm_wrapper/`: provider (sync, async, streaming), routing rules, redaction and restore, size checks, SQLite tracing, CLI. The whole suite (101 offline tests) passes with `.venv/bin/pytest -q` |
| Live validation (M1) | **Done** (2026-11-10). A live DeepSeek call redacted an email and a phone, restored both, streaming worked. Fastlanes stage 2 through `wrapper/auto`: 10 calls, task passed, the wrapper's token totals equal cppagent's exactly, the provider's `response_format` rejection reached cppagent unchanged. (The direct control run failed stage 2; with n = 1 each this is chance, not a wrapper effect) |
| Overhead (M2) | **Done.** `wrapper bench` on all 533 fastlanes prompts: p50 2.4 ms, p95 5.2 ms, p99 9.6 ms (after skipping exact token counts when a byte-length bound already fits; before: p95 38 ms) |
| Trace import | cppagent importer, plus a generic OpenAI-chat importer (`import-chat`), which turns each assistant turn into one call with its exact cached-prefix tokens |
| Phase-0 analysis | Run on fastlanes, 300 public coding-agent runs and 500 public chats (§3) |
| Prompt caching through the gateway | **Verified live.** The redacted prefix is byte-identical across turns, so DeepSeek cache hits continue (3,200 of about 3,370 tokens). Cached tokens are recorded and priced with the cache discount |
| Experiment engine | **Built** (`wrapper experiment run spec.yaml`): sampled replay, arms, scorers (`json_valid`, `similarity`, blind order-randomized judge with spend cap), control-arm comparison, promotion proposal. Replays reuse the caller's recorded params, including a recorded `response_format` schema. `--dry` estimates a run with no model calls; `--limit N` sets the sample size; arms can change the system prompt or transform the messages; scorer plugins (`python:module:fn`) and a `primary` scorer drive promotion. Reports carry bootstrap 95% CIs and a paired comparison with the control arm; promotion requires the CI to clear the bar |
| Run-level A/B (M5b) | **Built** (`wrapper ab run|report`): the caller's own command N times per arm under each arm's policy, calls tagged by `requests.run_tag`, success rate with Wilson intervals, cost per successful run, bootstrap CI vs the control. Tested offline with a child process and a fake model. The cppagent example in `examples/cppagent_ab/` is written but **not run** (it calls real models; needs the owner's go-ahead) |
| Gateway cost levers (M8, M10) | **Built.** `cache_control` markers survive redaction; cache-write tokens recorded and priced; opt-in `fallback:` retries once on the default model after a bad-request, not-found or auth error |
| Fine-tune export | **Built** (`wrapper export-finetune`): OpenAI chat JSONL, redacted with per-sample placeholders, train/holdout split stored so experiments evaluate on holdout only (`dataset.holdout_of`). Targets are logged answers (optionally successful runs only) or judge-preferred answers of a stronger arm (distillation). On fastlanes only 13–15 successful examples per role exist: too few to tune on (§3) |

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

### Gates
| # | Assumption | Test | Go if | Status |
|---|---|---|---|---|
| H1 | There is spend worth saving | `analyze` on real traffic: monthly spend, role mix, cache share | Monthly spend justifies the build at a 15% saving | **Open:** fastlanes is a test bed ($0.83). Production volume is needed |
| H2 | A cheaper model is good enough for a share of calls | Replay 300–500 logged calls on the cheap model; blind strong-model comparison with the logged answer; for cppagent also compile/test outcome | Equal or better on ≥ 40%, predictable from role or features | Open. First data (n = 20 per role): `gpt-6-luna` is not good enough for planner or repair. Needs ~100 samples, more candidate models and run-level outcomes (M5b) |
| H3 | Decomposition pays after prompt caching | Label 200 chat requests; cost planned vs. pass-through with caching | Lower cost on ≥ 10% of traffic at equal quality | **Fails for agent callers** (they plan themselves, and splitting would break a 98% cache prefix). Unlikely for chat (short prompts). Treat as dropped unless a caller shows otherwise |
| H4 | Redaction is accurate and harmless | Hand-label 200 records; detection precision and recall; 100 requests redacted vs. not | ≥ 95% recall on structured PII, < 1% false positives, no significant quality drop | Partly: five false-positive classes found on real traces and fixed. Recall and quality not measured |
| H5 | Enough volume to learn from | Requests per day per role or pattern | ≥ 50 per day for the top patterns | Open |
| H6 | Overhead is small | Benchmark pass-through with redaction | p95 under 30 ms | **Passed:** p95 5.2 ms on fastlanes prompts |

## 4. Scope
| In | Conditional (needs gate) | Out (separate project or later) |
|---|---|---|
| Pass-through gateway with privacy, size, tracing | Request decomposition (H3) | Response actions, approvals, undo |
| Policy routing by role, model or request features | Learned routing (H2 + H5) | Admin UI |
| Prompt-cache-aware cost model and exact response cache | ARI-style learned router (H5, then beats counts on replay) | Postgres, multi-tenant |
| Caller outcome feedback (task success) | LLM planner | OpenTelemetry export |
| Replay (dry and live) and acceptance gates | Similarity and classifier tiers | |

**Build vs. configure:** LiteLLM already has routers, fallbacks, budgets, caching and a
Presidio PII guardrail. Use them where they fit. Build only what they don't do: the
egress guarantee with restore and leak check, policy routing on caller context,
prompt-cache-aware cost, outcome-driven learning, and replay.

## 5. Architecture (as built, v1)
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
├── cli.py             init / validate / import-cppagent / import-chat / analyze / bench / experiment /
│                      export-finetune / delete / purge / health / show
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

## 7. Versions
Each version ships only when its acceptance criteria (§8) pass on the replay set and test suites.

### v1: Safe gateway (built; M1 and M2 done)
- **M1 live check (done):** fastlanes stage 2 through `wrapper/auto` on DeepSeek; see §2. A proper parity test needs several runs per arm, because one run each is dominated by model randomness.
- **M2 overhead benchmark (done):** `wrapper bench`, p95 5.2 ms.
- **M2b caching first (done):** the provider's cached tokens are recorded and priced, and placeholders and the hint keep prefixes byte-stable across turns (§2).
- **M3 proxy mode:** run under the LiteLLM proxy. Register a price for `wrapper/auto` so outer cost tracking works. Test that spend is counted once.
- **M4 caller integration:** cppagent passes `metadata={"role", "session_id", "task_id"}` (a small change in `cppagent/llm/lm.py`, which needs the owner's go-ahead). Document the same contract for other callers. (M5d is the same item.)

### v1.5: Replay and evidence (the platform loop)
- **M5 experiments (built):** live-sample replay with arms, scorers, control comparison, spend caps and promotion proposals. Still to add:
  - ~~a dry mode~~ (built: `experiment run SPEC --dry` reports refusals, reroutes, placeholders and estimated cost, with no model calls and nothing stored)
  - ~~confidence intervals and a paired test in the report~~ (built)
  - ~~larger samples for decisions~~ (built: `--limit N`; reports say "exploratory" below 100 scored samples. The default stays 20 because a run costs money)
- **M5b outcome scoring**, in two parts:
  - ~~a scorer plugin interface~~ (built: `python:module:fn(sample, answer) -> float | None`, scores stored per sample, mean, CI and paired difference per plugin, `primary:` picks the scorer the promotion bar uses)
  - ~~a run-level A/B harness~~ (built as `wrapper ab run|report`, with `requests.run_tag`, Wilson and bootstrap intervals; the cppagent example in `examples/cppagent_ab/` is written but not run, it needs the owner's go-ahead for the spend). Original description: run the caller's own command N times per arm, each arm with its own policy, and compare success rate and cost per successful run. This replaces the earlier idea of applying a single recorded patch, which can't work because traces don't hold the project state at the time of the call
- **M5c tuning arms:**
  - ~~prompt variants~~ (built: arm `system`, `system_file`, `system_suffix`; the judge sees the original request)
  - ~~context strategies~~ (built as a hook: arm `transform: python:module:fn`; `examples/transforms.py`. Summarizing strategies are the caller's to write)
  - fine-tuned or distilled models: export is built (`export-finetune`, holdout-aware). Still to do: submit a tuning job and run the holdout experiment. Gate: at least ~100 successful examples per role; fastlanes has 13–15, so distillation (a strong arm's judge-preferred answers) or more recorded traffic comes first
- **M5d recorder contract for callers (same as M4):** send `metadata={"role", "session_id", "task_id"}` and report outcomes, so experiments can slice by role and score by result.
- **M6 importers:** generic JSONL with field mapping, OpenAI and LiteLLM layouts. Public datasets only after license checks, sampled.
- **M7 analysis on production traffic:** decide H1, H2, H5. **Re-plan after M7.**

### v2: Cost levers that need no learning
- **M8 prompt-cache awareness (built):** stable prefixes, cached-token pricing, a test that `cache_control` markers pass through redaction untouched, the placeholder hint appended as a last part (not a new message) when the system content is a list, and cache-write tokens recorded (`requests.cache_write_tokens`) and priced at the provider's write rate.
- **M9 exact response cache:** keyed by redacted payload and model, only for `temperature == 0` or callers that opt in. cppagent already caches, so this is for other callers.
- **M10 error-aware fallback (built, opt-in, `fallback:` in the policy):** on a bad-request, not-found or auth error from the routed model, retry once on `models.default` (or `fallback.to`), re-prepared for the target's trust level, and record `fallback_from` / `fallback_error` and a `fallback` span. Never for timeouts, rate limits, context-window or content-policy errors, forced calls or streaming. Still to do: turn repeated fallbacks into route warnings.
- **M11 role routing:** policy routes by `metadata.role`, each with a replay gate. Fastlanes candidates: `planner` and `context` (short outputs, 100% parse rate) to a cheaper model; `repair` measured against its outcome.

### v3: Quality signal
- **M12 outcomes:** caller outcome feedback, validators (JSON schema, parse), behavior signals (needs `session_id`), unknown-placeholder count.
- **M13 sampled review:** a stronger model, in the background, with a daily cap, only where no caller outcome exists.
- **M14 optional local name detection,** if H4 shows that regex recall is too low.

### v4: Learning (only if H2 and H5 pass)
- **M15 stats:** per route or pattern, per exact model version, with **pooled estimates** across size buckets and model versions so that sparse cells borrow strength (review rev2 #4). Logged answers and caller outcomes count as labeled samples.
- **M16 learned router experiment:** an ARI-style small local scorer trained from preference pairs (reviewer comparisons and outcome pairs from exploration). It runs in background jobs, its weights are versioned, and it is evaluated on held-out replay. **It ships only if it beats pooled counts** on cost at equal quality. It can only rank candidates the policy allows.
- **M17 exploration** with a default-model floor inside the cap.
- **M18 proposals** and the approval flow for policy changes.

### Conditional: decomposition (only if H3 passes for chat callers)
The rev-2 design applies: two-stage pre-check, plan graph, caller system prompt in every step,
format-keeping combine. It never applies to callers that mark themselves as agents (`metadata.caller_kind: agent`).

## 8. Acceptance criteria
| Metric | Target | From |
|---|---|---|
| Leaks | 0 in leak, injection and non-text suites | v1 |
| Caller params | 100% forwarded unchanged on pass-through (except redaction and the hint) | v1 |
| Live parity (M1) | same task success as direct; tokens within 5% | v1 |
| Overhead | p95 under 30 ms added | v1 |
| Proxy spend | counted exactly once | v1 |
| Restore | 100% of vault placeholders restored in the restore suite | v1 |
| Redaction false positives | under 1% on the labeled sample | v1.5 |
| Cost | ≥ 15% lower than direct at equal quality on live-sample replay (not dry estimates) | v2 |
| Quality | not significantly worse than direct (paired test, p < 0.05), or equal caller outcome rate | v2 onward |
| Learned router | lower cost than pooled counts at equal quality on held-out replay | v4 |

## 9. Risks
| Risk | Mitigation |
|---|---|
| The wrapper is a single point of failure for every LLM call | Fail-open option per route (`on_wrapper_error: direct`) that skips routing but never privacy. Health command; timeouts |
| Redaction lowers answer quality | Measured in H4. Trusted local model for sensitive routes. Code blocks skipped by default |
| Regex misses names and free text | Stated plainly: the goal is reduced exposure, not a guarantee. Local name detection in v3 if needed |
| The local DB concentrates real data | 0600 file, retention and purge, delete by value, SQLCipher option, `record_inputs: false` |
| Learning never converges at low volume | Gate H5; pooled estimates; the default model stays the baseline |
| Cheap model drift | Default-model floor in exploration; stats per exact model version |

## 10. Open questions
1. Production traffic: which callers besides cppagent, the monthly spend, and requests per day (H1, H5)?
2. ~~May M1 use the DeepSeek key in cppagent's `.env`?~~ Answered by use: the live checks ran with it (about $0.60 in total so far).
3. Is privacy a legal or contract requirement, or a preference? This decides how much H4 work is needed.
4. Which models count as cheap, default and trusted (local)?
5. What cost target per caller? Is 15% right?
6. Retention periods?

## 11. Changes from rev 2
| Source | Item | Where |
|---|---|---|
| Critique | Requirements not validated | §3 gates; build only after a gate passes |
| Critique | Prompt caching missing from cost | §3 evidence, §6 cost model, M8 |
| Critique | Scope too large | §1 non-goals, §4 scope table; actions removed; decomposition conditional |
| Critique | Build vs. configure | §4 |
| Critique | Privacy promise too strong | §1 goal wording, §9 |
| Critique | Missing operations requirements | §9 fail-open, M2, M3 |
| Rev-2 review | 1 tool results masked | §6 trust vs. origin (built) |
| Rev-2 review | 2 hooks and streaming | §6 |
| Rev-2 review | 3 unknown params | §6 caller params (built) |
| Rev-2 review | 4 data too sparse | M15 pooled estimates; M16 |
| Rev-2 review | 5 milestone contradiction | Logged-answer replay moved to v1.5 (M5) |
| Rev-2 review | 6 provenance too coarse | Deferred with actions; to be applied per turn if injection gates return |
| Rev-2 review | 7 sync loop | §6 (built) |
| Rev-2 review | 8 multiple writers | §6 |
| Rev-2 review | 9 Router config | §6 internal model config |
| Rev-2 review | 10 delete by value | §6 (built) |
| Rev-2 review | 11 hint vs. pass-through | §8 caller params criterion; hint only when placeholders exist (built) |
| Rev-2 review | 12 `min_success` meaning | §6 |
| Rev-2 review | 13 exploration floor | §6 |
| Rev-2 review | 14 dry vs. live cost | §8 cost measured on live-sample replay |
| Rev-2 review | 15 conversation key | §5 flow step 2 |
| ARI post | Learned router | M16, as an experiment against pooled counts |
| Fastlanes | Agent callers plan themselves | §1, §3, conditional decomposition |
| Fastlanes | Repair dominates spend; caller outcome is the best label | §6 quality signal, M11, M12 |
| Fastlanes | Config errors | M10 error-aware fallback |
| Fastlanes | Card false positive | §3 (fixed) |

## 12. Next work
The ordered task list, with acceptance tests per task, is in `HANDOFF.md`.
