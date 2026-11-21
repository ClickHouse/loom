"""Retirement hides cleanup-derived diagnostics before reading the private queue."""

from __future__ import annotations

from typing import Any

import pytest

from server.core import diagnostics, request_work
from server.knowledge.app import source_retirements as cleanup
from server.knowledge.store import source_retirements as queue
from server.knowledge.workers import source_retirements as worker
from server.observability import events
from server.workers import pass_context, status
from tests.unit.test_knowledge_source_retirements import _item, _put
from tests.unit.test_knowledge_source_retirements import work as work
from tests.unit.test_private_diagnostics import isolated_context as isolated_context
from tests.unit.test_private_diagnostics import spans as spans


@pytest.mark.parametrize("case", ["empty", "retire", "failure"])
async def test_retirement_boundary_precedes_queue_and_omits_root_span(
    work: Any, monkeypatch: pytest.MonkeyPatch, spans: Any, case: str
) -> None:
    _, exporter = spans
    if case != "empty":
        _put(work, _item(), "private-document")
    original_page = queue.page
    calls: list[bool] = []

    async def page(after: tuple[str, str]) -> Any:
        calls.append(diagnostics.codes_only())
        assert diagnostics.codes_only()
        assert request_work.current() is None
        if case == "failure":
            raise RuntimeError("private queue error")
        return await original_page(after)

    monkeypatch.setattr(queue, "page", page)
    monkeypatch.setattr(status, "_registry", status.WorkerStatusRegistry())
    emitter = events.EventEmitter()
    monkeypatch.setattr(events, "get_emitter", lambda: emitter)
    bag = request_work.begin()
    before = emitter.stats()
    with pass_context.ensure_pass(cleanup.WORKER) as ctx:
        if case == "failure":
            with pytest.raises(RuntimeError, match="private queue error"):
                await worker.run_pass()
        else:
            await worker.run_pass()
        assert ctx.private_work
        if case == "retire":
            assert work.retired and work.cleared == [_item().source_id]
            assert work.cursor  # the operational cursor survives the private boundary
    assert calls == [True]
    shown = status.get_registry().snapshot()[cleanup.WORKER]
    assert shown["last_duration_ms"] is None
    assert shown["last_outcome"] == {"code": "SYNC_FAILED" if case == "failure" else "ok"}
    assert bag.ch_statements == 0 and emitter.stats() == before
    assert exporter.get_finished_spans() == ()
    assert not diagnostics.codes_only()
