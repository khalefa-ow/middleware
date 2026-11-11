"""LiteLLM custom provider: ``model="wrapper/auto"``.

The core is async. The sync ``completion``/``streaming`` entry points run the async path
on one long-lived background event loop (not a fresh loop per call), so they also work
when called from inside a running loop and background work is never cut off.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, AsyncIterator, Iterator

from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError

from .pipeline import SelfCallError, Wrapper
from .policy import PolicyError
from .privacy import EgressBlocked, NonTextBlocked
from .size import ContextTooLarge

_CLIENT_ERRORS = (EgressBlocked, NonTextBlocked, ContextTooLarge, SelfCallError, PolicyError)


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="wrapper-loop", daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float | None = None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


_LOOP: _LoopThread | None = None
_LOOP_LOCK = threading.Lock()


def _loop() -> _LoopThread:
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None:
            _LOOP = _LoopThread()
        return _LOOP


def _split(optional_params: dict, litellm_params: dict | None) -> tuple[dict, dict]:
    params = dict(optional_params or {})
    metadata = dict((litellm_params or {}).get("metadata") or {})
    params.pop("metadata", None)
    return params, metadata


def _raise_as_litellm(exc: Exception):
    if isinstance(exc, _CLIENT_ERRORS):
        raise CustomLLMError(status_code=400, message=f"{type(exc).__name__}: {exc}") from exc
    raise exc


class WrapperProvider(CustomLLM):
    def __init__(self, wrapper_factory) -> None:
        super().__init__()
        self._factory = wrapper_factory
        self._wrapper: Wrapper | None = None
        self._lock = threading.Lock()

    @property
    def wrapper(self) -> Wrapper:
        with self._lock:
            if self._wrapper is None:
                self._wrapper = self._factory()
            return self._wrapper

    # -- async
    async def acompletion(self, model, messages, api_base, custom_prompt_dict, model_response, print_verbose,
                          encoding, api_key, logging_obj, optional_params, acompletion=None, litellm_params=None,
                          logger_fn=None, headers={}, timeout=None, client=None):
        params, metadata = _split(optional_params, litellm_params)
        try:
            return await self.wrapper.acomplete(messages, params, metadata)
        except Exception as exc:
            _raise_as_litellm(exc)

    async def astreaming(self, model, messages, api_base, custom_prompt_dict, model_response, print_verbose,
                         encoding, api_key, logging_obj, optional_params, acompletion=None, litellm_params=None,
                         logger_fn=None, headers={}, timeout=None, client=None) -> AsyncIterator[dict]:
        params, metadata = _split(optional_params, litellm_params)
        try:
            async for chunk in self.wrapper.astream(messages, params, metadata):
                yield chunk
        except Exception as exc:
            _raise_as_litellm(exc)

    # -- sync
    def completion(self, model, messages, api_base, custom_prompt_dict, model_response, print_verbose, encoding,
                   api_key, logging_obj, optional_params, acompletion=None, litellm_params=None, logger_fn=None,
                   headers={}, timeout=None, client=None):
        params, metadata = _split(optional_params, litellm_params)
        try:
            return _loop().run(self.wrapper.acomplete(messages, params, metadata))
        except Exception as exc:
            _raise_as_litellm(exc)

    def streaming(self, model, messages, api_base, custom_prompt_dict, model_response, print_verbose, encoding,
                  api_key, logging_obj, optional_params, acompletion=None, litellm_params=None, logger_fn=None,
                  headers={}, timeout=None, client=None) -> Iterator[dict]:
        params, metadata = _split(optional_params, litellm_params)
        agen = self.wrapper.astream(messages, params, metadata)
        lt = _loop()
        while True:
            try:
                yield lt.run(agen.__anext__())
            except StopAsyncIteration:
                return
            except Exception as exc:
                _raise_as_litellm(exc)


def default_policy_path() -> str:
    return os.environ.get("WRAPPER_POLICY", "~/.llm_wrapper/policy.yaml")
