"""Every setting is reachable from its `LOOM_<FIELD>` variable, and a
malformed value is refused by name.

These hold for all fields at once, so a new setting is covered without
anyone listing it:

- setting `LOOM_<FIELD>` to a non-default value lands exactly that value on
  the field, for every bool, int, float, Literal, Optional and plain-string
  field;
- an unparseable value for any bool, int or float field fails construction,
  and the error names that field.

A few fields are checked together by model validators (the signing keys,
the LLM provider, the default namespace). They take structured or paired
values and have their own tests, so the sweep leaves them out.
"""

from __future__ import annotations

import types
import typing
from typing import Any

import annotated_types
import pytest
from pydantic import ValidationError
from pydantic.fields import FieldInfo

from server.settings import Settings
from server.settings._env import JsonArray, JsonObject, RoutePrior
from server.settings.catalog import BackoffSteps

_FIELDS = Settings.model_fields

# Fields a model validator reads together with other fields; each has its
# own tests (test_identity_mode, test_llm_provider_inference,
# test_default_namespace*).
_CROSS_CHECKED = {
    # Its sweep value would exceed half the pass deadline (test_conflict_resolver.py).
    "memory_conflict_resolver_burst_seconds",
    # Checked against each other (test_model_calls_switch.py).
    "model_calls_policy_refresh_seconds",
    "model_calls_policy_max_stale_seconds",
    "default_namespace",
    "jwt_public_keys_json",
    # Read with the issuer and the federated keys (test_sharing.py).
    "jwt_end_user_issuers_json",
    "llm_model",
    "llm_provider",
    # Names and URLs checked by a model validator (test_rerank_names.py).
    "rerank_endpoints_json",
    # URLs checked by a model validator (test_knowledge_extractors.py).
    "document_extractor_url",
    "document_extractors_json",
    "document_extractor_tokens_json",
    # URLs checked by a model validator (test_llm_gateway.py).
    "openai_base_url",
    "anthropic_base_url",
    "inference_token_url",
    # Read with the API roots and the embedder (test_llm_gateway.py).
    "inference_gateway_required",
    "inference_gateway_hosts",
    # Read with the token URL, the public URL and the JWT audience and issuers
    # (test_inference_identity_token.py).
    "inference_token_audience",
    # Read with the token file, the token URL and the gateway (test_llm_gateway_bearer.py).
    "inference_workload_token_mode",
    # A template that must contain {org}, checked by a model validator
    # (test_model_call_customer.py).
    "inference_customer_format",
    # A URL checked by a model validator (test_oidc_backchannel.py).
    "oidc_backchannel_base_url",
    # Host lists checked by a model validator (test_llm_gateway.py).
    "memory_classify_private_hosts",
    "memory_webhook_private_hosts",
}


def _default(f: FieldInfo) -> Any:
    return f.get_default(call_default_factory=True)


def _bounded(f: FieldInfo, value: float) -> bool:
    for m in f.metadata:
        if isinstance(m, annotated_types.Le) and value > m.le:
            return False
        if isinstance(m, annotated_types.Lt) and value >= m.lt:
            return False
        if isinstance(m, annotated_types.Ge) and value < m.ge:
            return False
        if isinstance(m, annotated_types.Gt) and value <= m.gt:
            return False
    return True


# Strings of one fixed form (a field pattern), given a valid value of that form.
_VALID = {"cell_router_token": "lcr_" + "0" * 128}


def _other(name: str, f: FieldInfo) -> Any:
    """A valid value for `f` that differs from its default, or None."""
    if name in _VALID:
        return _VALID[name]
    if name in _CROSS_CHECKED:
        return None
    ann, default = f.annotation, _default(f)
    origin, args = typing.get_origin(ann), typing.get_args(ann)
    if origin is typing.Literal:
        return next((a for a in args if a != default), None)
    if origin in (typing.Union, types.UnionType) and type(None) in args:
        inner = next(a for a in args if a is not type(None))
        if default is None:
            return {bool: True, int: 3, float: 0.25, str: "probe"}.get(inner)
        ann = inner
    if ann is bool:
        return not default
    if ann is int:
        return next((v for v in (default + 1, default - 1) if _bounded(f, v)), None)
    if ann is float:
        return next((v for v in (default + 0.25, default / 2) if _bounded(f, v)), None)
    if ann is str:
        if JsonArray in f.metadata:
            return "[]"
        if JsonObject in f.metadata or RoutePrior in f.metadata:
            return "{}" if default != "{}" else '{"probe": 1}'
        if BackoffSteps in f.metadata:
            return "1,2"
        return f"probe-{name}"
    return None


def _env(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    import os

    for key in [k for k in os.environ if k.startswith("LOOM_")]:
        monkeypatch.delenv(key)
    return monkeypatch


def test_the_cross_checked_strings_are_settings() -> None:
    assert _CROSS_CHECKED <= set(_FIELDS)


def test_every_env_variable_reaches_its_field(clean_env: pytest.MonkeyPatch) -> None:
    want = {n: v for n, f in _FIELDS.items() if (v := _other(n, f)) is not None}
    assert len(want) > 0.9 * (len(_FIELDS) - len(_CROSS_CHECKED)), "sweep lost its coverage"
    for name, value in want.items():
        clean_env.setenv(f"LOOM_{name.upper()}", _env(value))
    got = Settings(_env_file=None)  # type: ignore[call-arg]
    missed = [
        f"{n}: set {v!r}, read {getattr(got, n)!r}" for n, v in want.items() if getattr(got, n) != v
    ]
    assert not missed, "\n".join(missed)


def test_an_unparseable_number_or_flag_is_refused_by_name(clean_env: pytest.MonkeyPatch) -> None:
    typed = [n for n, f in _FIELDS.items() if f.annotation in (bool, int, float)]
    for name in typed:
        clean_env.setenv(f"LOOM_{name.upper()}", "not-a-value")
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)  # type: ignore[call-arg]
    refused = {str(err["loc"][0]) for err in caught.value.errors()}
    assert sorted(set(typed) - refused) == []


def _lower_bound(f: FieldInfo) -> float | None:
    for m in f.metadata:
        if isinstance(m, annotated_types.Ge):
            return float(m.ge)  # type: ignore[arg-type]
    return None


def test_a_value_below_a_declared_floor_is_refused_by_name(clean_env: pytest.MonkeyPatch) -> None:
    """A documented bound is declared on the field, so an out-of-range value
    fails at load instead of being clamped where it is read."""
    floored = {
        n: lb
        for n, f in _FIELDS.items()
        if f.annotation in (int, float) and (lb := _lower_bound(f)) is not None
    }
    assert {"namespace_gc_batch", "session_brief_window_chars", "tracker_embed_batch"} <= set(
        floored
    )
    for name, lb in floored.items():
        below = int(lb) - 1 if _FIELDS[name].annotation is int else lb - 0.5
        clean_env.setenv(f"LOOM_{name.upper()}", str(below))
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)  # type: ignore[call-arg]
    refused = {str(err["loc"][0]) for err in caught.value.errors()}
    assert sorted(set(floored) - refused) == []
