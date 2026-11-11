# cppagent under the A/B harness

Compares policies by running cppagent's own command several times per arm (`wrapper ab`), then comparing
the success rate and the cost per successful run. A single recorded call can't be scored by outcome; a run can.

Files
- `ab.yaml`: the spec (two arms: `deepseek` = control, `luna` = every call to `openai/gpt-6-luna`)
- `policies/base.yaml`, `policies/luna_default.yaml`: the arms' policies (the second is an overlay)
- `run.sh RUN_DIR POLICY`: runs one cppagent run (`scripts/run_cppagent_with_wrapper.py`, cppagent's venv)
- `cfg/`: the fastlanes task, trimmed to stages 1 and 2; `"output_dir": "../work"` gives each run its own work folder

Run (NOT run yet)
```bash
.venv/bin/wrapper --db var/ab.db ab run examples/cppagent_ab/ab.yaml
.venv/bin/wrapper --db var/ab.db ab report fastlanes-ab
```
Needs cmake, patch and a compiler, and cppagent's own venv (`$CPPAGENT/.venv`; `CPPAGENT` defaults to
`/mnt/data/agent-workspace/projects/cppagent`). Credentials come from the environment, or from cppagent's `.env`,
which cppagent loads itself; the wrapper never stores them.

Cost: on 2026-11-10 one stage-2 run made 11 model calls for about $0.023; stage 1 also calls the model, so a run
costs roughly $0.02-0.10. The default is 5 runs per arm, about $0.5-1.0 in total; `budget_usd: 1.5` stops the
experiment early (remaining runs are marked skipped, not failed).

Open point: the starting state. Without `SEED_DIR` every run starts from an empty folder and does stage 1 as well,
so a stage-1 failure counts against the arm. For a stage-2-only comparison, run stage 1 once, copy the resulting
work folder somewhere, and set `SEED_DIR` in `ab.yaml`'s `env:` to it, so every run starts from the same state.

Results are only as good as the number of runs: with 5 runs per arm only large differences are detectable.
