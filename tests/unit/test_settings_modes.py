"""Mode settings load as a closed set: each Literal is exactly what its reader
accepts, a value is case-folded first, and anything else fails at load."""

from __future__ import annotations

import logging
import typing

import pytest
from pydantic import ValidationError

from server.settings import Settings


def _modes(field: str) -> set[str]:
    return set(typing.get_args(Settings.model_fields[field].annotation))


def _wire(model: type, field: str) -> set[str]:
    """The string values a request field's Literal (or Optional Literal) admits, minus ""."""
    ann = model.model_fields[field].annotation  # type: ignore[attr-defined]
    arms = [ann] if typing.get_origin(ann) is typing.Literal else list(typing.get_args(ann))
    args = {a for arm in arms for a in typing.get_args(arm) if isinstance(a, str)}
    return args - {""}


def test_code_modes_match_the_request_fields() -> None:
    from server.code.api.models_search import CodeSearchInput

    assert _modes("code_search_llm_legs_default") == _wire(CodeSearchInput, "llm_legs")
    assert _modes("code_search_card_leg_mode") == {"skeleton", "best_chunk"}


def test_memory_modes_match_the_request_fields() -> None:
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.api.models_search import MemorySearchInput

    assert _modes("memory_search_default_mode") == _wire(MemorySearchInput, "search_mode")
    assert _modes("memory_search_floor_mode") == _wire(MemorySearchInput, "floor_mode")
    assert _modes("memory_ask_world_knowledge") == _wire(MemoryAskInput, "world_knowledge")
    assert _modes("memory_ask_deep_evidence_select") == _wire(MemoryAskInput, "deep_select")
    assert _modes("facts_traverse_rank_by") == _wire(FactsTraverseInput, "rank_by")


def test_platform_modes_match_their_readers() -> None:
    from server.core.vectors.patch_parts import codes_first_mode
    from server.transport.mcp_schemas import SCHEMA_PROFILES

    assert _modes("mcp_schema_profile") == set(SCHEMA_PROFILES)
    assert all(codes_first_mode(m) == m for m in _modes("memory_search_fact_codes_first"))
    assert all(isinstance(logging.getLevelName(m.upper()), int) for m in _modes("log_level"))
    assert _modes("otel_exporter") == {"", "ch", "otlp"}
    assert _modes("langfuse_capture_io_level") == {"off", "query", "all"}
    assert _modes("memory_extract_observation_date_default") == {"request_time", "none"}


def test_every_embedding_provider_mode_is_one_the_embedder_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.embedding import embedder
    from server.settings import settings

    for mode in _modes("embedding_provider") - {""}:
        monkeypatch.setattr(settings, "embedding_provider", mode)
        assert embedder.provider_for("some-model") == mode


def test_a_mode_is_case_folded_and_an_unknown_one_fails_at_load() -> None:
    s = Settings(_env_file=None, log_level=" INFO ", memory_search_key_axis="Off")  # type: ignore[call-arg]
    assert (s.log_level, s.memory_search_key_axis) == ("info", "off")
    with pytest.raises(ValidationError, match="memory_search_default_mode"):
        Settings(_env_file=None, memory_search_default_mode="semantc")  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="memory_search_key_axis"):
        Settings(_env_file=None, memory_search_key_axis="true")  # type: ignore[call-arg]


def test_a_retire_confidence_below_the_clear_confidence_fails_at_load() -> None:
    with pytest.raises(ValidationError, match="FLAG_CONFIDENCE must lie between"):
        Settings(
            memory_conflict_resolver_retire_confidence=0.85,
            memory_conflict_resolver_clear_confidence=0.90,
        )
    Settings(
        memory_conflict_resolver_retire_confidence=0.90,
        memory_conflict_resolver_flag_confidence=0.90,
        memory_conflict_resolver_clear_confidence=0.90,
    )


@pytest.mark.parametrize("flag", [0.85, 0.99])
def test_a_flag_confidence_outside_clear_and_retire_fails_at_load(flag: float) -> None:
    with pytest.raises(ValidationError, match="FLAG_CONFIDENCE must lie between"):
        Settings(memory_conflict_resolver_flag_confidence=flag)


@pytest.mark.parametrize("bound", [0, -1])
def test_the_set_chunk_row_bound_is_at_least_one(bound: int) -> None:
    # 0 rows would drop every long value's chunks; a negative one fails its bind
    with pytest.raises(ValidationError, match="memory_search_set_chunk_rows"):
        Settings(_env_file=None, memory_search_set_chunk_rows=bound)  # type: ignore[call-arg]
