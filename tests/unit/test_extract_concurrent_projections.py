"""The session digest is drafted while the extractor runs, and
the post-extraction projections are awaited together.

CH-free and model-free: every projection is a stub that records when it ran.
The assertions are about ORDER and OVERLAP, not about row shapes (those are
covered by test_session_summaries.py and the chunk/evidence tests).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_bulk import MemorySetFromMessagesInput
from server.memory.app.session_summary import SessionSummaryDraft

_DRAFT = SessionSummaryDraft(
    summary_text="User moved to Lisbon in 2021.",
    embedding=[0.1, 0.2, 0.3],
    embedding_model="fake-embed",
)


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
    draft: Any = _DRAFT,
    draft_raises: bool = False,
    chunks_raise: bool = False,
) -> None:
    import server.identity.app.namespace_ontology as ont
    import server.memory.app.extract.handler as mx
    import server.memory.app.pipeline as pipe
    from server.memory.app import bulk_set as _bulk_set_mod

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
    monkeypatch.setattr(
        mx,
        "_draft_session_summary",
        log.stage("draft", seconds=0.02, result=draft, raises=draft_raises),
    )
    monkeypatch.setattr(mx, "_link_written_memories_to_evidence", log.stage("link", result=3))
    monkeypatch.setattr(mx, "write_memory_chunks", log.stage("chunks", raises=chunks_raise))
    monkeypatch.setattr(mx, "_write_session_summary", log.stage("summary"))
    monkeypatch.setattr(mx, "record_input_projection_failure", _failure)
    monkeypatch.setattr(_bulk_set_mod, "record_input_projection_failure", _failure)
    monkeypatch.setattr(ont, "load_namespace_ontology", _cold)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@pytest.mark.asyncio
async def test_draft_runs_during_extraction_and_projections_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx

    log = _Log()
    _wire(monkeypatch, log)
    out = await mx.handle_memory_set_from_messages(_input())

    # The digest draft started before the pipeline finished.
    assert log.spans["draft"][0] < log.spans["pipeline"][1]
    # The three projections ran together, not back to back.
    assert _overlap(log.spans["link"], log.spans["chunks"])
    assert _overlap(log.spans["chunks"], log.spans["summary"])
    # The pre-made draft reached the writer; nothing was drafted twice.
    assert log.drafts_written == [_DRAFT]
    assert log.calls.count("draft") == 1
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
async def test_failed_draft_is_recorded_and_not_redrafted(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.extract.handler as mx

    log = _Log()
    _wire(monkeypatch, log, draft_raises=True)
    out = await mx.handle_memory_set_from_messages(_input())

    assert log.failures == ["session_summaries"]
    assert "summary" not in log.calls  # no second model call on the inline path
    assert out.written


@pytest.mark.asyncio
async def test_empty_draft_skips_the_row_without_a_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx

    log = _Log()
    _wire(monkeypatch, log, draft=None)
    await mx.handle_memory_set_from_messages(_input())

    assert log.calls.count("draft") == 1
    assert "summary" not in log.calls
    assert log.failures == []


@pytest.mark.asyncio
async def test_extraction_failure_cancels_the_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.extract.handler as mx
    import server.memory.app.pipeline as pipe
    from server.memory.app.extract import ExtractionError

    log = _Log()
    _wire(monkeypatch, log)
    cancelled = asyncio.Event()

    async def _slow_draft(*args: Any, **kwargs: Any) -> Any:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _pipeline_raises(steps: Any, state: Any) -> Any:
        await asyncio.sleep(0)  # let the draft task start, as a real model call would
        raise ExtractionError("no JSON object in LLM response")

    monkeypatch.setattr(mx, "_draft_session_summary", _slow_draft)
    monkeypatch.setattr(pipe, "run_pipeline", _pipeline_raises)

    with pytest.raises(HTTPException) as ei:
        await mx.handle_memory_set_from_messages(_input())
    assert ei.value.status_code == 502
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)


def test_write_session_summary_uses_a_given_draft() -> None:
    """The writer's `draft` parameter is the seam the concurrent path relies on."""
    import inspect

    from server.memory.app.session_summary import _write_session_summary

    assert "draft" in inspect.signature(_write_session_summary).parameters
