import json
from typing import Any

import litellm
import pytest

from llm_wrapper.pipeline import Wrapper
from llm_wrapper.policy import policy_from_dict
from llm_wrapper.storage import Store

KEY = bytes(range(32))


def base_policy(**over: Any) -> dict:
    d = {"version": 1, "models": {"default": "openai/gpt-4o-mini"}}
    for k, v in over.items():
        d[k] = v
    return d


class FakeModel:
    """Stands in for litellm.acompletion: records outbound kwargs, returns scripted replies."""

    def __init__(self, replies=None, finish=None, tool_args=None):
        self.calls: list[dict] = []
        self.replies = list(replies or ["ok"])
        self.finish = list(finish or [])
        self.tool_args = tool_args

    async def __call__(self, **kw):
        self.calls.append(kw)
        if kw.get("stream"):
            return self._stream(self._reply_text())
        text = self._reply_text()
        fin = self.finish.pop(0) if self.finish else "stop"
        msg: dict = {"role": "assistant", "content": text}
        if self.tool_args is not None:
            msg["tool_calls"] = [{"id": "c1", "type": "function",
                                  "function": {"name": "send", "arguments": self.tool_args}}]
        return litellm.ModelResponse(model=kw["model"], choices=[{"message": msg, "finish_reason": fin}],
                                     usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    def _reply_text(self):
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    async def _stream(self, text):
        for i in range(0, len(text), 3):
            yield litellm.ModelResponseStream(choices=[{"delta": {"content": text[i:i + 3]}, "index": 0}])
        yield litellm.ModelResponseStream(choices=[{"delta": {}, "finish_reason": "stop", "index": 0}],
                                          usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})

    @property
    def sent(self) -> str:
        return json.dumps([{"messages": c["messages"], "tools": c.get("tools")} for c in self.calls])


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "w.db"))
    yield s
    s.close()


@pytest.fixture
def make_wrapper(store):
    def mk(fake: FakeModel | None = None, **policy):
        w = Wrapper(policy_from_dict(base_policy(**policy)), store, KEY)
        w.acompletion = fake or FakeModel()
        return w
    return mk
