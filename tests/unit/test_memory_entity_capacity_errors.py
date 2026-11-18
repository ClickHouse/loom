"""Entity expansion refuses oversized internal inputs without changing the filter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from loom.errors import ValidationError as SDKValidationError
from loom.errors import from_response

from server.core.errors import VectorInputTooLargeError, classified
from server.memory.api.models_search import MemorySearchInput, MemorySearchTraceInput
from server.memory.app import entity_resolution
from server.memory.app.search import computed, scope, trace
from tests.unit.ch_fake import RecordingCH


@pytest.mark.parametrize("entry", ["scope", "lookup", "trace"])
@pytest.mark.parametrize("repeated", [False, True], ids=["distinct", "repeated"])
async def test_internal_raw_filter_refuses_before_normalization_or_io(
    entry: str, repeated: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    entities = ["same"] * 65 if repeated else [f"entity{i:03d}" for i in range(65)]
    request = MemorySearchInput(org="o", namespace="n", query="q").model_copy(
        update={"entity_filter": entities}
    )
    ch = RecordingCH()

    def no_embedder():
        raise AssertionError("oversized input must not reach the embedder")

    monkeypatch.setattr(trace, "get_embedder", no_embedder)
    with pytest.raises(VectorInputTooLargeError):
        if entry == "scope":
            scope._entity_filter(request, [])
        elif entry == "lookup":
            await entity_resolution.lookup_canonical_entities(
                ch, org="o", namespaces=["n"], entity_texts=entities
            )
        else:
            traced = MemorySearchTraceInput(org="o", namespace="n", query="q").model_copy(
                update={"entity_filter": entities}
            )
            await trace.handle_memory_search_trace(traced)
    assert ch.calls == []


def test_parser_entities_obey_the_same_bound() -> None:
    request = MemorySearchInput(org="o", namespace="n", query="q")
    with pytest.raises(VectorInputTooLargeError):
        scope._entity_filter(request, ["same"] * 65)


@pytest.mark.parametrize("prelaunched", [False, True], ids=["serial", "prelaunched"])
async def test_search_scope_does_not_replace_refused_aliases_with_the_raw_filter(
    prelaunched: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = MemorySearchInput(org="o", namespace="n", query="q", entity_filter=["noah"])
    ch = RecordingCH().on(
        "FROM loom.entity_canonical_map",
        [{"canonical_text": f"commit:alias{i:03d}"} for i in range(257)],
    )
    calibration = asyncio.create_task(asyncio.sleep(0))
    await calibration
    run = scope._SearchRun("", None, calibration, None)
    monkeypatch.setattr(scope, "get_client", lambda: ch)
    if prelaunched:
        run.canonical_prelaunched = True
        run.effective_entity_filter = ["noah"]
        run.canonical_task = asyncio.create_task(
            entity_resolution.lookup_canonical_entities(
                ch, org="o", namespaces=["n"], entity_texts=["noah"]
            )
        )
    try:
        with pytest.raises(VectorInputTooLargeError):
            await scope._resolve_entity_scope(request, run, [], {})
        assert len(ch.calls) == 1
    finally:
        await run.close()


async def test_alias_backend_cannot_hide_a_typed_input_refusal() -> None:
    refusal = VectorInputTooLargeError("entity expansion is too large")

    def refuse(_sql: str, _params: dict) -> list[dict]:
        raise refusal

    ch = RecordingCH().on("FROM loom.entity_canonical_map", refuse)
    with pytest.raises(VectorInputTooLargeError) as raised:
        await entity_resolution.lookup_canonical_entities(
            ch, org="o", namespaces=["n"], entity_texts=["noah"]
        )
    assert raised.value is refusal


async def test_derived_canonical_refusal_is_not_an_empty_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = RecordingCH().on(
        "FROM loom.entity_canonical_map",
        [{"canonical_text": f"commit:alias{i:03d}"} for i in range(257)],
    )
    monkeypatch.setattr(computed, "get_client", lambda: ch)
    ctx = SimpleNamespace(
        input=MemorySearchInput(org="o", namespace="n", query="q"),
        derived_aggregate_cache={},
        query_shape="aggregational",
        stated_value_update=False,
        parser_derived_intent={"op": "count", "subject": "noah"},
        llm_free=True,
    )
    with pytest.raises(VectorInputTooLargeError):
        await computed._maybe_derived_aggregate(ctx)
    assert ctx.derived_aggregate_cache == {}
    assert len(ch.calls) == 1


def test_input_refusal_keeps_its_code_and_nonretryable_sdk_class() -> None:
    refusal = VectorInputTooLargeError("entity alias expansion exceeds 256 names")
    detail = classified(refusal.to_detail(), refusal.status_code)
    assert refusal.status_code == 422
    assert detail["code"] == "VECTOR_INPUT_TOO_LARGE"
    assert detail["category"] == "input" and detail["retryable"] is False
    error = from_response({"detail": detail}, refusal.status_code)
    assert isinstance(error, SDKValidationError)
    assert error.code == refusal.code and error.retryable is False
