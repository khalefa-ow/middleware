"""llm_wrapper: a LiteLLM custom provider exposed as ``model="wrapper/auto"``.

    import llm_wrapper
    llm_wrapper.register("policy.yaml")          # or set WRAPPER_POLICY
    litellm.completion(model="wrapper/auto", messages=[...])

For the LiteLLM proxy, point ``custom_provider_map`` at ``llm_wrapper.proxy_handler``.
"""

from __future__ import annotations

import os

__version__ = "0.1.0"

PROVIDER = "wrapper"


def build_wrapper(policy_path: str | None = None, db_path: str | None = None, overlays=()):
    from .pipeline import Wrapper
    from .policy import load_policy
    from .privacy import load_key
    from .provider import default_policy_path
    from .storage import Store

    loaded = load_policy(policy_path or default_policy_path(), list(overlays))
    store = Store(db_path or os.environ.get("WRAPPER_DB") or loaded.policy.storage.path)
    return Wrapper(loaded, store, load_key())


def register(policy_path: str | None = None, db_path: str | None = None, overlays=()):
    """Register ``wrapper/*`` with LiteLLM. The policy loads lazily on the first call."""
    import litellm

    from .provider import WrapperProvider

    handler = WrapperProvider(lambda: build_wrapper(policy_path, db_path, overlays))
    litellm.custom_provider_map = [p for p in litellm.custom_provider_map if p.get("provider") != PROVIDER]
    litellm.custom_provider_map.append({"provider": PROVIDER, "custom_handler": handler})
    from litellm.utils import custom_llm_setup

    custom_llm_setup()
    return handler


def __getattr__(name: str):
    if name == "proxy_handler":  # LiteLLM proxy: custom_handler: llm_wrapper.proxy_handler
        from .provider import WrapperProvider

        handler = WrapperProvider(build_wrapper)
        globals()["proxy_handler"] = handler
        return handler
    raise AttributeError(name)
