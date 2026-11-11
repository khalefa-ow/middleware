"""Policy: schema, loading, layering, secrets and validation.

A policy is one YAML file or a folder of YAML files (merged in name order). Layers merge
base -> environment overlay(s): maps merge, lists replace, and lists of items with an
``id`` merge by id. The effective policy is hashed; every request records that hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .expr import ExprError, compile_expr

WRAPPER_PREFIX = "wrapper/"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Models(_Strict):
    default: str
    large_context: list[str] = []
    """Larger-context models a too-big request may be rerouted to (non-lossy)."""


class CustomPattern(_Strict):
    id: str
    type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    regex: str


class Privacy(_Strict):
    enabled: bool = True
    trusted: list[str] = []
    """Models/agents that may receive real values (e.g. a local model)."""
    key_scope: Literal["conversation", "global"] = "conversation"
    response_values: Literal["input_only", "all"] = "input_only"
    non_text: Literal["pass", "block", "trusted_only"] = "trusted_only"
    patterns: list[str] = ["email", "phone", "card", "ssn", "api_key"]
    custom_patterns: list[CustomPattern] = []
    allow_patterns: list[str] = []
    redact_code_blocks: bool = False
    placeholder_hint: bool = True
    leak_check: bool = True


class Route(_Strict):
    id: str
    when: str
    model: str
    allow_lossy: bool = False

    @field_validator("when")
    @classmethod
    def _check_expr(cls, v: str) -> str:
        try:
            compile_expr(v)
        except ExprError as exc:
            raise ValueError(str(exc)) from exc
        return v


class Size(_Strict):
    margin: float = Field(0.10, ge=0, lt=0.9)
    reserve_output_tokens: int = 4096
    continue_output: bool = True
    max_continuations: int = Field(2, ge=0, le=10)
    reroute: bool = True
    limits: dict[str, int] = {}
    """Per-model max input tokens overriding LiteLLM's model info."""


class Limits(_Strict):
    call_timeout_s: float = 600
    max_depth: int = 1


class Retention(_Strict):
    requests_days: int = 90
    vault_days: int = 90


class Storage(_Strict):
    path: str = "~/.llm_wrapper/wrapper.db"
    record_inputs: bool = True


class Fallback(_Strict):
    """Opt-in: one retry on ``to`` when the routed model fails on a configuration error."""
    enabled: bool = False
    on: list[Literal["bad_request", "not_found", "auth"]] = ["bad_request", "not_found", "auth"]
    to: str | None = None
    """Fallback model; null means ``models.default``."""

    @model_validator(mode="before")
    @classmethod
    def _yaml_on_key(cls, data: Any) -> Any:
        """An unquoted ``on:`` is the boolean True in YAML 1.1; read it as the ``on`` key."""
        if isinstance(data, dict) and True in data:
            data = dict(data)
            if "on" in data:
                raise ValueError("fallback has both 'on' and a boolean key (an unquoted 'on:' is read as true by YAML)")
            data["on"] = data.pop(True)
        return data


class Policy(_Strict):
    version: int = 1
    models: Models
    privacy: Privacy = Privacy()
    routes: list[Route] = []
    size: Size = Size()
    limits: Limits = Limits()
    retention: Retention = Retention()
    storage: Storage = Storage()
    fallback: Fallback = Fallback()

    @model_validator(mode="after")
    def _no_self_calls(self) -> "Policy":
        targets = [self.models.default, *self.models.large_context, *(r.model for r in self.routes)]
        if self.fallback.to:
            targets.append(self.fallback.to)
        for t in targets:
            if t.startswith(WRAPPER_PREFIX):
                raise ValueError(f"target model {t!r} would call the wrapper itself")
        ids = [r.id for r in self.routes]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate route ids: {sorted(dup)}")
        return self

    def is_trusted(self, model: str) -> bool:
        return model in self.privacy.trusted


# ---------------------------------------------------------------- loading


_SECRET = re.compile(r"\$\{(ENV|FILE):([^}]+)\}")


class PolicyError(ValueError):
    pass


def resolve_secrets(value: Any) -> Any:
    """Replace ${ENV:NAME} / ${FILE:path} references. Resolved values are never traced."""
    if isinstance(value, dict):
        return {k: resolve_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secrets(v) for v in value]
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            kind, ref = m.group(1), m.group(2)
            if kind == "ENV":
                if ref not in os.environ:
                    raise PolicyError(f"secret ${{ENV:{ref}}} is not set")
                return os.environ[ref]
            p = Path(ref).expanduser()
            if not p.is_file():
                raise PolicyError(f"secret file {p} does not exist")
            return p.read_text().strip()
        return _SECRET.sub(sub, value)
    return value


def merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = merge(base[k], v) if k in base else v
        return out
    if (
        isinstance(base, list) and isinstance(over, list)
        and all(isinstance(i, dict) and "id" in i for i in base + over)
    ):
        out_l = {i["id"]: i for i in base}
        for i in over:
            out_l[i["id"]] = merge(out_l[i["id"]], i) if i["id"] in out_l else i
        return list(out_l.values())
    return over


def _read_layers(path: Path) -> list[dict]:
    files = sorted(path.glob("*.y*ml")) if path.is_dir() else [path]
    layers = []
    for f in files:
        try:
            data = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError as exc:
            raise PolicyError(f"{f}: {exc}") from exc
        if not isinstance(data, dict):
            raise PolicyError(f"{f}: top level must be a mapping")
        layers.append(data)
    if not layers:
        raise PolicyError(f"no policy files found at {path}")
    return layers


class LoadedPolicy(BaseModel):
    policy: Policy
    hash: str
    effective_yaml: str
    source: str


def load_policy(path: str | os.PathLike, overlays: list[str | os.PathLike] = ()) -> LoadedPolicy:
    p = Path(path).expanduser()
    raw: dict = {}
    for layer in _read_layers(p) + [l for o in overlays for l in _read_layers(Path(o).expanduser())]:
        raw = merge(raw, layer)
    effective_yaml = yaml.safe_dump(raw, sort_keys=True)
    try:
        policy = Policy.model_validate(resolve_secrets(raw))
    except PolicyError:
        raise
    except Exception as exc:
        raise PolicyError(f"invalid policy {p}:\n{exc}") from exc
    digest = hashlib.sha256(effective_yaml.encode()).hexdigest()[:16]
    return LoadedPolicy(policy=policy, hash=digest, effective_yaml=effective_yaml, source=str(p))


def policy_from_dict(data: dict) -> LoadedPolicy:
    policy = Policy.model_validate(resolve_secrets(data))
    eff = yaml.safe_dump(data, sort_keys=True)
    return LoadedPolicy(policy=policy, hash=hashlib.sha256(eff.encode()).hexdigest()[:16],
                        effective_yaml=eff, source="<dict>")


def policy_json_schema() -> str:
    return json.dumps(Policy.model_json_schema(), indent=2)
