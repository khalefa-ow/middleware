"""Child process for the A/B harness tests: two gateway calls against a fake model, then exit.

Policy and database come from WRAPPER_POLICY / WRAPPER_DB (set by the harness). CHILD_EXIT is the exit code,
CHILD_PRINT is written to stdout.
"""

import os
import sys

import litellm

import llm_wrapper
from tests.conftest import FakeModel

handler = llm_wrapper.register()
handler.wrapper.acompletion = FakeModel()
for i in range(2):
    litellm.completion(model="wrapper/auto", messages=[{"role": "user", "content": f"hello {i}"}])
print(os.environ.get("CHILD_PRINT", ""))
sys.exit(int(os.environ.get("CHILD_EXIT", "0")))
