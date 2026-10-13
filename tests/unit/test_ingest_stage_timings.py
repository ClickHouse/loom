"""An ingest names its stages, and the bulk write's tail legs
(verdict settlement, audit, the four graph projections) run together.

CH-free: the ClickHouse client, embedder and every projection are stubs
that record when they ran.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app import session_summary as _session_summary_mod
from server.memory.app.extract import handler as mx


class _StubCH:
    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        await asyncio.sleep(0.01)


class _StubEmbedder:
    model_name = "fake-test"
    dim = 3

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


class _Spans:
    def __init__(self) -> None:
        self.spans: dict[str, tuple[float, float]] = {}

    def leg(self, name: str, seconds: float = 0.05, *, raises: bool = False):
        async def _run(*args: Any, **kwargs: Any) -> None:
            t0 = time.perf_counter()
            await asyncio.sleep(seconds)
            self.spans[name] = (t0, time.perf_counter())
            if raises:
                raise RuntimeError(f"{name} exploded")

        return _run


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _wire(
    monkeypatch: pytest.MonkeyPatch, spans: _Spans, *, edges_raise: bool = False
) -> list[str]:

    failures: list[str] = []
    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: _StubCH())
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: _StubCH())
    monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: _StubEmbedder())
    monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: _StubEmbedder())

    async def _none(*_: Any, **__: Any) -> None:
        return None

    async def _resolve(*_: Any, **__: Any) -> dict[str, str]:
        return {}

    async def _failure(input: Any, *, projection: str, **kwargs: Any) -> None:
        failures.append(projection)

    monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _none)
    monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _none)
    monkeypatch.setattr("server.memory.app.entity_resolution.resolve_entities_for_write", _resolve)
    monkeypatch.setattr("server.memory.store.insert_embedding_rows", _none)
    monkeypatch.setattr(_bulk_set_mod, "_audit_bulk_write", spans.leg("audit"))
    monkeypatch.setattr(_bulk_set_mod, "write_entity_vectors", spans.leg("entity_vectors"))
    monkeypatch.setattr(
        _bulk_set_mod, "write_entity_edges", spans.leg("entity_edges", raises=edges_raise)
    )
    monkeypatch.setattr(_bulk_set_mod, "write_entity_triples", spans.leg("entity_triples"))
    monkeypatch.setattr(_bulk_set_mod, "_write_derived_facts", spans.leg("derived_facts"))
    monkeypatch.setattr(mx, "record_input_projection_failure", _failure)
    monkeypatch.setattr(_bulk_set_mod, "record_input_projection_failure", _failure)
    return failures


def _input() -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="test",
        namespace="ns",
        agent="a",
        user_id="u",
        session_id="s",
        entries=[
            _BulkSetEntry(
                memory_key="home",
                value="Lisbon",
                memory_type="semantic",
                entities=["Lisbon"],
                relations=[("user", "lives_in", "Lisbon")],
            ),
            _BulkSetEntry(memory_key="pet", value="a dog called Rex", memory_type="semantic"),
        ],
    )


@pytest.mark.asyncio
async def test_bulk_tail_legs_overlap_and_phases_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    spans = _Spans()
    failures = _wire(monkeypatch, spans)
    out = await mx.handle_memory_bulk_set(_input())

    assert [w.memory_key for w in out.written] == ["home", "pet"]
    assert _overlap(spans.spans["audit"], spans.spans["entity_vectors"])
    assert _overlap(spans.spans["entity_edges"], spans.spans["derived_facts"])
    for phase in (
        "existing_lookup",
        "entity_resolution",
        "prepare",
        "embed",
        "verdicts",
        "insert",
        "value_updates",
        "projections",
        "total",
    ):
        assert phase in out.stage_ms, phase
    # The four legs took ~50 ms each; together they cost about one leg.
    assert out.stage_ms["projections"] < 150.0
    assert failures == []


@pytest.mark.asyncio
async def test_one_failing_leg_is_recorded_and_the_rest_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _Spans()
    failures = _wire(monkeypatch, spans, edges_raise=True)
    out = await mx.handle_memory_bulk_set(_input())

    assert failures == ["entity_edges"]
    assert {"audit", "entity_vectors", "entity_triples", "derived_facts"} <= set(spans.spans)
    assert len(out.written) == 2


@pytest.mark.asyncio
async def test_pipeline_records_a_time_per_step(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.pipeline as pipe
    from server.memory.app import pipeline_state as pipeline_state

    async def _sleepy(state: Any, config: dict[str, Any]) -> Any:
        await asyncio.sleep(0.02)
        return state

    async def _quick(state: Any, config: dict[str, Any]) -> Any:
        return state

    monkeypatch.setitem(pipe._REGISTRY, "test.sleepy", _sleepy)
    monkeypatch.setitem(pipe._REGISTRY, "test.quick", _quick)
    state = pipeline_state.WorkflowState(org="o", namespace="n")
    state = await pipe.run_pipeline(
        [
            pipeline_state.PipelineStep(step_id="test.sleepy"),
            pipeline_state.PipelineStep(step_id="test.quick"),
            pipeline_state.PipelineStep(step_id="test.unknown"),
        ],
        state,
    )
    assert state.stage_ms["step.test.sleepy"] >= 15.0
    assert 0.0 <= state.stage_ms["step.test.quick"] < 15.0
    assert "step.test.unknown" in state.stage_ms  # an unknown step is timed too
    assert "unknown_step:test.unknown" in state.errors


@pytest.mark.asyncio
async def test_bulk_write_step_carries_bulk_phases_onto_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.pipeline as pipe
    from server.memory.api.models_bulk import MemoryBulkSetOutput, _BulkSetResult
    from server.memory.app import bulk_set as bulk_set_mod
    from server.memory.app import pipeline_state as pipeline_state

    async def _fake_bulk(bulk_input: Any) -> MemoryBulkSetOutput:
        return MemoryBulkSetOutput(
            written=[_BulkSetResult(memory_key="home", version=1)],
            stage_ms={"embed": 12.5, "total": 40.0},
        )

    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", _fake_bulk)
    state = pipeline_state.WorkflowState(
        org="o", namespace="n", extracted_facts=[{"key": "home", "value": "Lisbon"}]
    )
    state = await pipe._step_bulk_write(state, {})
    assert [(w["memory_key"], w["version"], w["superseded_by"]) for w in state.written] == [
        ("home", 1, "")
    ]
    # The whole result rides along, so set_from_messages reports the verdict too.
    assert {"conflict_verdict", "peers", "next"} <= set(state.written[0])
    assert state.stage_ms["bulk.embed"] == 12.5 and state.stage_ms["bulk.total"] == 40.0


class _CountingEmbedder(_StubEmbedder):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


class _CapturingCH(_StubCH):
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.inserts.append((table, rows))


@pytest.mark.asyncio
async def test_entity_texts_ride_the_value_embedding_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entity-vector projection makes no embedding call of its own."""

    ch = _CapturingCH()
    embedder = _CountingEmbedder()
    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: embedder)
    monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: embedder)

    async def _none(*_: Any, **__: Any) -> None:
        return None

    async def _resolve(*_: Any, **__: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _none)
    monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _none)
    monkeypatch.setattr("server.memory.app.entity_resolution.resolve_entities_for_write", _resolve)
    monkeypatch.setattr("server.memory.store.insert_embedding_rows", _none)
    monkeypatch.setattr(_bulk_set_mod, "_audit_bulk_write", _none)
    monkeypatch.setattr(_bulk_set_mod, "write_entity_edges", _none)
    monkeypatch.setattr(_bulk_set_mod, "write_entity_triples", _none)
    monkeypatch.setattr(_bulk_set_mod, "_write_derived_facts", _none)

    out = await mx.handle_memory_bulk_set(_input())
    assert len(out.written) == 2
    # One provider batch: values + keys + entities together.
    assert len(embedder.calls) == 1
    assert any("lisbon" in t.lower() for t in embedder.calls[0])
    ev = [rows for table, rows in ch.inserts if table == "loom.entity_vectors"]
    assert ev and ev[0][0]["entity_embedding"] == [0.1, 0.2, 0.3]
    assert {r["entity_text"].lower() for r in ev[0]} >= {"lisbon"}
