from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError as PydanticValidationError

from server.core.errors import LoomServerError
from server.memory.api.models_search import MemorySearchInput, MemorySearchTraceInput
from server.memory.app.entity_resolution import lookup_canonical_entities
from tests.unit.ch_fake import RecordingCH


@pytest.mark.parametrize(
    "model", [MemorySearchInput, MemorySearchTraceInput], ids=["search", "trace"]
)
@pytest.mark.parametrize("count", [0, 1, 64], ids=["empty", "one", "raw64"])
def test_supported_entity_filter_keeps_input_order(
    model: type[MemorySearchInput], count: int
) -> None:
    entities = [f"entity{i:03d}" for i in reversed(range(count))]

    request = model(org="o", namespace="n", query="q", entity_filter=entities)

    assert request.entity_filter == entities
    assert request.model_dump()["entity_filter"] == entities


@pytest.mark.parametrize(
    "model", [MemorySearchInput, MemorySearchTraceInput], ids=["search", "trace"]
)
@pytest.mark.parametrize("repeated", [False, True], ids=["distinct", "repeated"])
def test_entity_filter_refuses_raw65_before_deduplication(
    model: type[MemorySearchInput], repeated: bool
) -> None:
    entities = ["same"] * 65 if repeated else [f"entity{i:03d}" for i in range(65)]

    with pytest.raises(PydanticValidationError) as raised:
        model(org="o", namespace="n", query="q", entity_filter=entities)

    errors = raised.value.errors(include_url=False)
    assert len(errors) == 1
    assert errors[0]["loc"] == ("entity_filter",)
    assert errors[0]["type"] == "too_long"
    assert errors[0]["ctx"]["max_length"] == 64
    assert errors[0]["ctx"]["actual_length"] == 65


async def test_empty_entity_lookup_does_not_read() -> None:
    ch = RecordingCH()

    async with asyncio.timeout(5):
        result = await lookup_canonical_entities(ch, org="o", namespaces=["n"], entity_texts=[])

    assert result == []
    assert ch.calls == []


async def test_alias_lookup_keeps_cited_order_scope_and_sorted_new_aliases() -> None:
    ch = RecordingCH().on(
        "FROM loom.entity_canonical_map",
        [
            {"canonical_text": "zoey"},
            {"canonical_text": "aaron"},
            {"canonical_text": "zoey"},
            {"canonical_text": "noah"},
        ],
    )

    async with asyncio.timeout(5):
        result = await lookup_canonical_entities(
            ch,
            org="o",
            namespaces=["z", "a", "z"],
            entity_texts=["noah", "aurora", "bart", "noah"],
        )

    assert result == ["noah", "aurora", "bart", "aaron", "zoey"]
    assert len(ch.calls) == 1
    call = ch.calls[0]
    assert call.kind == "query_all"
    assert call.params == {
        "org": "o",
        "namespaces": ["a", "z"],
        "mentions": ["aurora", "bart", "noah"],
    }
    assert "org = {org:String}" in call.sql
    assert "namespace IN {namespaces:Array(String)}" in call.sql
    assert "tombstoned_at IS NULL" in call.sql


async def test_alias_union_accepts_exactly256_and_preserves_order() -> None:
    aliases = [f"commit:alias{i:03d}" for i in range(255)]
    ch = RecordingCH().on(
        "FROM loom.entity_canonical_map",
        [{"canonical_text": value} for value in reversed(aliases)],
    )

    async with asyncio.timeout(5):
        result = await lookup_canonical_entities(
            ch, org="o", namespaces=["n"], entity_texts=["noah"]
        )

    assert result == ["noah", *aliases]
    assert len(result) == 256
    assert len(ch.calls) == 1


async def test_alias_rows256_are_allowed_when_union_stays256() -> None:
    aliases = [f"commit:alias{i:03d}" for i in range(255)]
    ch = RecordingCH().on(
        "FROM loom.entity_canonical_map",
        [{"canonical_text": value} for value in ["noah", *aliases]],
    )

    async with asyncio.timeout(5):
        result = await lookup_canonical_entities(
            ch, org="o", namespaces=["n"], entity_texts=["noah"]
        )

    assert result == ["noah", *aliases]
    assert len(result) == 256
    assert len(ch.calls) == 1


async def test_alias_union257_refuses_instead_of_truncating_or_returning_raw() -> None:
    aliases = [f"commit:alias{i:03d}" for i in range(256)]
    ch = RecordingCH().on(
        "FROM loom.entity_canonical_map",
        [{"canonical_text": value} for value in aliases],
    )

    with pytest.raises(LoomServerError) as raised:
        async with asyncio.timeout(5):
            await lookup_canonical_entities(ch, org="o", namespaces=["n"], entity_texts=["noah"])

    assert raised.value.code == "VECTOR_INPUT_TOO_LARGE"
    assert len(ch.calls) == 1
    assert all(call.kind == "query_all" for call in ch.calls)


async def test_alias_rows257_refuse_even_if_normalization_would_fit() -> None:
    # Distinct database strings can normalize to the same entity. A full sentinel
    # page cannot prove that omitted aliases would fit the effective bound.
    aliases = ["same", " Same ", "SAME", *[f"commit:alias{i:03d}" for i in range(254)]]
    ch = RecordingCH().on(
        "FROM loom.entity_canonical_map",
        [{"canonical_text": value} for value in aliases],
    )

    with pytest.raises(LoomServerError) as raised:
        async with asyncio.timeout(5):
            await lookup_canonical_entities(ch, org="o", namespaces=["n"], entity_texts=["noah"])

    assert raised.value.code == "VECTOR_INPUT_TOO_LARGE"
    assert len(aliases) == 257
    assert len(set(aliases)) == 257
    assert len(ch.calls) == 1


async def test_alias_read_requests_distinct_rows_and_the257_sentinel() -> None:
    ch = RecordingCH().on("FROM loom.entity_canonical_map", [])

    async with asyncio.timeout(5):
        result = await lookup_canonical_entities(
            ch, org="o", namespaces=["n"], entity_texts=["noah"]
        )

    assert result == ["noah"]
    assert len(ch.calls) == 1
    call = ch.calls[0]
    sql = " ".join(call.sql.split())
    assert "SELECT DISTINCT canonical_text" in sql
    assert "LIMIT 257" in sql


async def test_ordinary_alias_backend_failure_retains_existing_raw_order() -> None:
    def unavailable(sql: str, params: dict) -> list[dict]:
        raise RuntimeError("alias reader unavailable")

    ch = RecordingCH().on("FROM loom.entity_canonical_map", unavailable)

    async with asyncio.timeout(5):
        result = await lookup_canonical_entities(
            ch, org="o", namespaces=["n"], entity_texts=["noah", "aurora", "noah"]
        )

    assert result == ["noah", "aurora"]
    assert len(ch.calls) == 1
