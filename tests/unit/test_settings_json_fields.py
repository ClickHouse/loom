"""JSON-valued settings are checked at load: a value that is not JSON, or
whose top level is the wrong shape, fails naming the field and never
printing the value. The readers still parse the entries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.settings import Settings
from server.settings._env import JsonArray, JsonObject, RoutePrior

_OBJECTS = [
    "ch_settings_by_traffic_class",
    "llm_policy_by_traffic_class",
    "ratelimit_class_fractions",
    "rerank_endpoints_json",
    "code_vector_tables",
    "latency_budget_overrides_by_consumer",
    "tracker_sources_json",
    "tracker_credentials_json",
    "tracker_code_namespace_bindings_json",
    "tracker_restricted_repos_json",
]
_ARRAYS = ["code_repos_json", "code_test_gate_run_templates"]
_CANARY = "s3cr3t-canary"


def _load(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_the_checked_fields_are_the_json_valued_ones() -> None:
    fields = Settings.model_fields
    assert {n for n, f in fields.items() if JsonObject in f.metadata} == set(_OBJECTS)
    assert {n for n, f in fields.items() if JsonArray in f.metadata} == set(_ARRAYS)
    assert [n for n, f in fields.items() if RoutePrior in f.metadata] == [
        "memory_ask_synth_route_prior"
    ]


# Object fields whose entries a model validator also checks (test_rerank_names.py), so
# the arbitrary object below is not a valid value for them.
_ENTRIES_CHECKED = {"rerank_endpoints_json"}


@pytest.mark.parametrize("field", [f for f in _OBJECTS if f not in _ENTRIES_CHECKED])
def test_an_object_field_takes_empty_or_an_object(field: str) -> None:
    assert getattr(_load(**{field: ""}), field) == ""
    assert getattr(_load(**{field: ' {"a": {}} '}), field) == ' {"a": {}} '
    for bad in (f'{{"a": "{_CANARY}"', f'["{_CANARY}"]'):
        with pytest.raises(ValidationError, match=field) as caught:
            _load(**{field: bad})
        assert _CANARY not in str(caught.value)


@pytest.mark.parametrize("field", _ARRAYS)
def test_an_array_field_takes_empty_or_an_array(field: str) -> None:
    assert getattr(_load(**{field: '[{"a": 1}]'}), field) == '[{"a": 1}]'
    with pytest.raises(ValidationError, match="must be a JSON array"):
        _load(**{field: '{"a": 1}'})


def test_the_route_prior_also_takes_its_named_values() -> None:
    for ok in ("", "measured", "Measured", "off", '{"identifier": 0.2}'):
        assert _load(memory_ask_synth_route_prior=ok).memory_ask_synth_route_prior == ok
    with pytest.raises(ValidationError, match="memory_ask_synth_route_prior"):
        _load(memory_ask_synth_route_prior="measurd")


def test_every_default_loads() -> None:
    s = _load()
    assert s.ratelimit_class_fractions == '{"bench": 0.5}'
