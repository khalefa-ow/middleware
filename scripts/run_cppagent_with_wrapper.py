"""Run cppagent's run_task.py with ``wrapper/auto`` registered.

    PYTHONPATH=$CPPAGENT/usecase $CPPAGENT/.venv/bin/python scripts/run_cppagent_with_wrapper.py \
        POLICY.yaml --config CFG/task.json --model wrapper/auto --no-cache

CPPAGENT defaults to /mnt/data/agent-workspace/projects/cppagent (override with the CPPAGENT env var).
llm_wrapper must be importable in cppagent's venv (add this repo to PYTHONPATH as well).
"""
import os
import runpy
import sys

import llm_wrapper

CPPAGENT = os.environ.get("CPPAGENT", "/mnt/data/agent-workspace/projects/cppagent")
llm_wrapper.register(sys.argv[1])
sys.argv = ["run_task.py", *sys.argv[2:]]
runpy.run_path(os.path.join(CPPAGENT, "usecase", "run_task.py"), run_name="__main__")
