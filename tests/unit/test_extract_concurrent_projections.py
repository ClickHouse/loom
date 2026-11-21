"""The session digest is drafted while the extractor runs, and
the post-extraction projections are awaited together.

CH-free and model-free: every projection is a stub that records when it ran.
The assertions are about ORDER and OVERLAP, not about row shapes (those are
covered by test_session_summaries.py and the chunk/evidence tests).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server.memory.api.models_bulk import MemorySetFromMessagesInput


@pytest.fixture(autouse=True)
def _no_refused_intake(monkeypatch):
    # Pipeline and projection cases; actual refusal admission is tested separately.
    monkeypatch.setattr("server.memory.store.refusals.check_intake", AsyncMock())


def _input() -> MemorySetFromMessagesInput:
    return MemorySetFromMessagesInput(
        org="dev",
        namespace="ns",
        agent="a",
        session_id="s1",
        messages=[{"role": "user", "content": "I moved to Lisbon in 2021."}],
    )


class _Log:
    """Wall-clock spans per stage, so the tests can say what overlapped."""

    def __init__(self) -> None:
        self.spans: dict[str, tuple[float, float]] = {}
        self.calls: list[str] = []
        self.failures: list[str] = []
        self.drafts_written: list[Any] = []

    def stage(self, name: str, *, seconds: float = 0.05, result: Any = None, raises: bool = False):
        async def _run(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            self.calls.append(name)
            await asyncio.sleep(seconds)
            self.spans[name] = (t0, time.perf_counter())
            if raises:
                raise RuntimeError(f"{name} exploded")
            if name == "summary":
                self.drafts_written.append(kwargs.get("draft"))
            return result

        return _run


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    log: _Log,
    *,
    summary_raises: bool = False,
    chunks_raise: bool = False,
) -> None:
    import server.identity.app.namespace_ontology as ont
    import server.memory.app.extract.handler as mx
    import server.memory.app.pipeline as pipe
    from server.memory.app import bulk_set as _bulk_set_mod

    monkeypatch.setattr(
        mx, "get_client", lambda: SimpleNamespace(query_all=AsyncMock(return_value=[]))
    )

    async def _no_evidence(input: Any, msgs: Any, **kw: Any) -> list[str]:
        return ["ev-1"]

    async def _pipeline(steps: Any, state: Any) -> Any:
        t0 = time.perf_counter()
        log.calls.append("pipeline")
        await asyncio.sleep(0.05)
        state.extracted_facts = [{"key": "home", "value": "Lisbon"}]
        state.written = [{"memory_key": "home", "version": 1}]
        log.spans["pipeline"] = (t0, time.perf_counter())
        return state

    async def _failure(input: Any, *, projection: str, **kwargs: Any) -> None:
        log.failures.append(projection)

    async def _cold(org: str, ns: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(mx, "_write_source_evidence", _no_evidence)
    monkeypatch.setattr(pipe, "run_pipeline", _pipeline)
    monkeypatch.setattr(mx, "_link_written_memories_to_evidence", log.stage("link", result=3))
    monkeypatch.setattr(mx, "write_memory_chunks", log.stage("chunks", raises=chunks_raise))
    monkeypatch.setattr(mx, "_write_session_summary", log.stage("summary", raises=summary_raises))
    monkeypatch.setattr(mx, "record_input_projection_failure", _failure)
    monkeypatch.setattr(_bulk_set_mod, "record_input_projection_failure", _failure)
    monkeypatch.setattr(ont, "load_namespace_ontology", _cold)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@pytest.mark.asyncio
async def test_committed_extraction_precedes_overlapping_projections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx

    log = _Log()
    _wire(monkeypatch, log)
    out = await mx.handle_memory_set_from_messages(_input())

    assert log.spans["summary"][0] >= log.spans["pipeline"][1]
    # The three projections ran together, not back to back.
    assert _overlap(log.spans["link"], log.spans["chunks"])
    assert _overlap(log.spans["chunks"], log.spans["summary"])
    assert out.belief_source_links == 3
    assert [w.memory_key for w in out.written] == ["home"]
    assert log.failures == []


@pytest.mark.asyncio
async def test_one_failing_projection_does_not_take_the_others_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx

    log = _Log()
    _wire(monkeypatch, log, chunks_raise=True)
    out = await mx.handle_memory_set_from_messages(_input())

    assert log.failures == ["memory_chunks"]
    assert "summary" in log.spans and "link" in log.spans
    assert out.belief_source_links == 3
    assert out.written


@pytest.mark.asyncio
async def test_a_summary_projection_failure_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.extract.handler as mx

    log = _Log()
    _wire(monkeypatch, log, summary_raises=True)
    out = await mx.handle_memory_set_from_messages(_input())

    assert log.failures == ["session_summaries"]
    assert log.calls.count("summary") == 1
    assert out.written


async def test_extraction_failure_never_starts_a_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.extract.handler as mx
    import server.memory.app.pipeline as pipe
    from server.memory.app.extract import ExtractionError

    log = _Log()
    _wire(monkeypatch, log)

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise ExtractionError("no JSON object in LLM response")

    monkeypatch.setattr(pipe, "run_pipeline", fail)
    with pytest.raises(HTTPException) as error:
        await mx.handle_memory_set_from_messages(_input())
    assert error.value.status_code == 502
    assert "summary" not in log.calls
