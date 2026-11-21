"""Source rotation and live privacy policy remain valid across awaited work."""

import time
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.knowledge.workers import query_history as worker
from server.workers import pass_context


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> Any:
    row = {
        "org": "o",
        "namespace": "n",
        "name": "🙂" * 100,
        "source_id": str(uuid.uuid4()),
        "tombstoned_at": None,
    }
    monkeypatch.setattr(worker.settings, "knowledge_query_history_enabled", True)
    monkeypatch.setattr(worker.settings, "knowledge_query_history_min_users", 3)
    monkeypatch.setattr(worker, "require_draft_fleet", AsyncMock())
    monkeypatch.setattr(worker, "namespace_over_cap", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "erasure_since", AsyncMock(return_value=False))
    monkeypatch.setattr(worker.repo, "find", AsyncMock(return_value=row))
    monkeypatch.setattr(worker, "already_claimed", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "emit_worker_cost_event_for", lambda **kw: None)
    pass_context.begin("query_history")
    yield row
    pass_context.clear()


@pytest.mark.asyncio
async def test_unicode_source_name_cannot_truncate_rotation(
    source: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker.repo, "active_after", AsyncMock(return_value=[source]))
    monkeypatch.setattr(worker, "_source", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "record_pass", lambda name: nullcontext(SimpleNamespace()))
    await worker.run_pass()
    saved = pass_context.current().cursor
    assert saved == source["source_id"]
    pass_context.begin("query_history", resume_cursor=saved)
    assert worker._cursor() == source["source_id"]
    await worker.run_pass()
    assert worker.repo.active_after.await_args.args[0] == source["source_id"]


@pytest.mark.asyncio
async def test_exhausted_rotation_persists_a_nonempty_start_cursor(
    source: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pass_context.begin("query_history", resume_cursor=source["source_id"])
    monkeypatch.setattr(worker.repo, "active_after", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "record_pass", lambda name: nullcontext(SimpleNamespace()))
    await worker.run_pass()
    saved = pass_context.current().cursor
    assert saved == str(uuid.UUID(int=0))
    pass_context.begin("query_history", resume_cursor=saved)
    assert worker._cursor() == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("during_model", [False, True])
async def test_live_threshold_increase_invalidates_inflight_group(
    source: Any,
    monkeypatch: pytest.MonkeyPatch,
    during_model: bool,
) -> None:
    group = {"query_hash": 1, "template_hash": 2, "users": 3, "sql": "SELECT sum(x) FROM t"}

    async def read(row: Any) -> Any:
        if not during_model:
            monkeypatch.setattr(worker.settings, "knowledge_query_history_min_users", 4)
        return [group]

    async def draft(*args: Any, **kwargs: Any) -> str:
        assert await kwargs["still_valid"]()
        monkeypatch.setattr(worker.settings, "knowledge_query_history_min_users", 4)
        assert not await kwargs["still_valid"]()
        return "candidate"

    model = AsyncMock(side_effect=draft)
    monkeypatch.setattr(worker, "read_groups", read)
    monkeypatch.setattr(worker, "draft", model)
    await worker._source(source, time.monotonic() + 60)
    assert model.await_count == int(during_model)


@pytest.mark.asyncio
async def test_gap_draft_invalidates_evidence_when_live_threshold_increases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_gaps import KnowledgeGapsDraftInput
    from server.knowledge.app import gap_drafts

    monkeypatch.setattr(gap_drafts.settings, "knowledge_gaps_enabled", True)
    monkeypatch.setattr(gap_drafts.settings, "knowledge_gaps_min_askers", 3)
    monkeypatch.setattr(
        gap_drafts.capture_consent, "level_uncached", AsyncMock(return_value="full")
    )
    monkeypatch.setattr(gap_drafts.gaps, "erasure_since", AsyncMock(return_value=False))
    monkeypatch.setattr(
        gap_drafts.repo, "gap_for_draft", AsyncMock(return_value={"head": "revenue", "samples": []})
    )
    monkeypatch.setattr(gap_drafts.repo, "draft_queries", AsyncMock(return_value=["revenue"]))

    async def draft(*args: Any, **kwargs: Any) -> str:
        assert await kwargs["still_valid"]()
        monkeypatch.setattr(gap_drafts.settings, "knowledge_gaps_min_askers", 4)
        assert not await kwargs["still_valid"]()
        return "candidate"

    monkeypatch.setattr(gap_drafts, "draft", draft)
    result = await gap_drafts.handle_gap_draft(
        KnowledgeGapsDraftInput(org="o", namespace="n", gap_id="g")
    )
    assert result.candidate_id == "candidate"


async def test_source_stops_at_the_configured_draft_limit(
    source: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker.settings, "knowledge_query_history_max_drafts", 1)
    groups = [
        {"query_hash": i, "template_hash": i, "users": 3, "sql": "SELECT x FROM t"} for i in [1, 2]
    ]
    monkeypatch.setattr(worker, "read_groups", AsyncMock(return_value=groups))
    model = AsyncMock(return_value="candidate")
    monkeypatch.setattr(worker, "draft", model)
    assert await worker._source(source, time.monotonic() + 30) == 1
    assert model.await_count == 1


async def test_pass_bounds_source_selection_and_starts_no_work_after_its_deadline(
    source: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(worker.settings, "knowledge_query_history_max_sources", 2)
    monkeypatch.setattr(worker.settings, "knowledge_query_history_max_pass_seconds", 6.0)

    async def loaded(*args: Any) -> Any:
        clock["now"] = 104.0
        return [source]

    monkeypatch.setattr(worker.repo, "active_after", AsyncMock(side_effect=loaded))
    turn = AsyncMock(return_value=0)
    monkeypatch.setattr(worker, "_source", turn)
    monkeypatch.setattr(worker, "record_pass", lambda name: nullcontext(SimpleNamespace()))
    assert await worker.run_pass() == {"sources": 0, "attempted": 0}
    assert worker.repo.active_after.await_args.args[1] == 2
    turn.assert_not_awaited()


def test_query_history_registry_uses_the_configured_cadence_and_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers.registry import enabled_workers

    monkeypatch.setattr(worker.settings, "knowledge_query_history_enabled", True)
    monkeypatch.setattr(worker.settings, "knowledge_query_history_interval_seconds", 1234)
    monkeypatch.setattr(worker.settings, "knowledge_query_history_max_pass_seconds", 90.0)
    spec = next(s for s in enabled_workers() if s.name == "query_history")
    assert spec.interval_seconds == 1234
    assert spec.max_pass_seconds == 90.0
    assert spec.pass_callable is worker.run_pass
