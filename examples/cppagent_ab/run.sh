#!/usr/bin/env bash
# One cppagent run under the wrapper, for `wrapper ab run examples/cppagent_ab/ab.yaml`.
# usage: run.sh RUN_DIR POLICY
#   RUN_DIR   a fresh folder for this run (the harness creates it)
#   POLICY    the arm's effective policy file
# Optional: SEED_DIR=<folder> copies that folder to RUN_DIR/work first, so every run starts from the same
# state (for example a snapshot taken after stage 1 passed; cppagent then continues at stage 2).
# Credentials come from the environment, or from cppagent's own .env (cppagent loads it itself).
set -euo pipefail
RUN_DIR=$(cd "$1" && pwd)
POLICY=$2
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
export CPPAGENT=${CPPAGENT:-/mnt/data/agent-workspace/projects/cppagent}

cp -r "$HERE/cfg" "$RUN_DIR/cfg"            # task.json has "output_dir": "../work" -> RUN_DIR/work
if [ -n "${SEED_DIR:-}" ]; then
  mkdir -p "$RUN_DIR/work"
  cp -a "$SEED_DIR/." "$RUN_DIR/work/"
fi
export PYTHONPATH="$REPO:$CPPAGENT/usecase${PYTHONPATH:+:$PYTHONPATH}"
exec "$CPPAGENT/.venv/bin/python" "$REPO/scripts/run_cppagent_with_wrapper.py" "$POLICY" \
  --config "$RUN_DIR/cfg/task.json" --model wrapper/auto --no-cache
