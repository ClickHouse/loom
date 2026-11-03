"""JSON-valued settings are checked at load: a value that is not JSON, or
whose top level is the wrong shape, fails naming the field and never
printing the value."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.settings import Settings
from server.settings._env import JsonArray, JsonObject, RoutePrior

_OBJECTS = [
    "ch_settings_by_traffic_class",
    "llm_policy_by_traffic_class",
    "llm_max_tokens_overrides",
    "ratelimit_class_fractions",
    "rerank_endpoints_json",
    "document_extractors_json",
    "document_extractor_tokens_json",
    "inference_feature_map_json",
    "inference_identity_headers_json",
    "inference_identity_refusals_json",
    "code_vector_tables",
    "latency_budget_overrides",
    "latency_budget_overrides_by_consumer",
    "tracker_sources_json",
    "tracker_credentials_json",
    "tracker_code_namespace_bindings_json",
    "tracker_restricted_repos_json",
]
# An object whose entries the field also checks needs a sample with real entries.
_OBJECT_SAMPLES = {
    "llm_max_tokens_overrides": ' {"hyde": 64} ',
    "latency_budget_overrides": ' {"memory.search": "2x"} ',
    "rerank_endpoints_json": ' {"gw": "https://rerank.example/v1"} ',
    "document_extractors_json": ' {"pdf": "http://pdf-extractor:8790"} ',
    "document_extractor_tokens_json": ' {"http://extractor:8790": "token"} ',
    "inference_feature_map_json": ' {"hyde": "query-expansion"} ',
    "inference_identity_headers_json": ' {"X-Agent": "{agent}"} ',
    "inference_identity_refusals_json": ' {"wait": [{"status": 503, "type": "api_error"}]} ',
}
# What a sample needs beside it to load.
_COMPANIONS = {
    "inference_identity_headers_json": {"inference_gateway_hosts": "gateway.example"},
    "document_extractor_tokens_json": {"document_extractor_url": "http://extractor:8790"},
}
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


@pytest.mark.parametrize("field", _OBJECTS)
def test_an_object_field_takes_empty_or_an_object(field: str) -> None:
    assert getattr(_load(**{field: ""}), field) == ""
    sample = _OBJECT_SAMPLES.get(field, ' {"a": {}} ')
    assert getattr(_load(**{field: sample}, **_COMPANIONS.get(field, {})), field) == sample
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
