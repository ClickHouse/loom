"""Private queue evidence cannot outlive a concurrent namespace erasure."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.core import leases
from server.core.errors import BackendUnavailableError, ValidationError
from server.erasure.store import steps_ledgers
from server.memory.api.models_learnings import LearningsReviewInput
from server.memory.app import definition_review, learnings
from tests.unit.definition_queue_fake import install

PROPOSED = datetime(2026, 1, 1)
VALUE = json.dumps({"term": "Revenue", "rests_on": {"sql": "SELECT private_sales"}})


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> Any:
    state = SimpleNamespace(
        rows=[],
        fence=install(monkeypatch),
        entered=asyncio.Event(),
        resume=asyncio.Event(),
        pause=False,
        failure=None,
    )
    candidate = {
        "candidate_id": "c",
        "org": "o",
        "namespace": "n",
        "memory_key": "definition/c",
        "memory_type": "definition",
        "proposed_value": VALUE,
        "source_kind": "query_history",
        "status": "pending",
        "version": 1,
        "supporters": [],
        "rationale": "private SQL",
        "proposed_at": PROPOSED,
    }
    state.rows.append(candidate)

    async def find(*args: Any) -> Any:
        return dict(state.rows[-1]) if state.rows else None

    async def insert(row: Any) -> None:
        if state.pause:
            state.entered.set()
            await state.resume.wait()
        if state.failure:
            raise state.failure
        state.rows.append(dict(row))

    async def delete(*args: Any, **kw: Any) -> int:
        count = len(state.rows)
        state.rows.clear()
        return count

    @asynccontextmanager
    async def claim(*args: Any, **kw: Any) -> Any:
        yield True

    monkeypatch.setattr(leases, "holding", claim)
    monkeypatch.setattr(learnings, "private_candidates_visible", AsyncMock(return_value=True))
    monkeypatch.setattr(definition_review, "require_draft_fleet", AsyncMock())
    monkeypatch.setattr(learnings.learnings_repo, "find_candidate", find)
    monkeypatch.setattr(learnings.learnings_repo, "insert_row", insert)
    monkeypatch.setattr(steps_ledgers, "_delete_many", delete)
    return state


async def reject() -> Any:
    return await learnings.handle_learnings_review(
        LearningsReviewInput(org="o", namespace="n", candidate_id="c", decision="reject")
    )


async def erase(queue: Any) -> int:
    queue.fence.erasures = [{"scope_type": "namespace", "scope_value": "n", "purge": True}]
    return await steps_ledgers.delete_learnings_queue_by_namespace("o", "n")


@pytest.mark.parametrize("kind", ["query_history", "knowledge_gap"])
async def test_namespace_erasure_cannot_finish_before_an_admitted_review_send(
    queue: Any, kind: str
) -> None:
    queue.rows[0]["source_kind"] = kind
    queue.pause = True
    review = asyncio.create_task(reject())
    try:
        await asyncio.wait_for(queue.entered.wait(), 1)
        # A final census cannot certify this deletion while a decision can still land.
        with pytest.raises(BackendUnavailableError):
            await erase(queue)
    finally:
        queue.resume.set()
        await review
    await erase(queue)
    assert queue.rows == []


@pytest.mark.parametrize("kind", ["query_history", "knowledge_gap"])
async def test_review_read_before_completed_erasure_cannot_restore_candidate(
    queue: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    queue.rows[0]["source_kind"] = kind
    find = learnings.learnings_repo.find_candidate
    reads = 0

    async def stale(*args: Any) -> Any:
        nonlocal reads
        row = await find(*args)
        reads += 1
        if reads == 2:
            await erase(queue)
        return row

    monkeypatch.setattr(learnings.learnings_repo, "find_candidate", stale)
    with pytest.raises(ValidationError, match="eras"):
        await reject()
    assert queue.rows == []
    assert queue.fence.reads[-1][3] == int(PROPOSED.replace(tzinfo=UTC).timestamp())


async def test_private_review_refuses_an_unreadable_erasure_ledger(queue: Any) -> None:
    queue.fence.failure = RuntimeError("ledger unavailable")
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await reject()
    assert len(queue.rows) == 1 and queue.rows[0]["status"] == "pending"


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_uncertain_review_send_keeps_eraser_out_until_its_holder_expires(
    queue: Any, failure: type[BaseException]
) -> None:
    queue.failure = failure()
    with pytest.raises(failure):
        await reject()
    with pytest.raises(BackendUnavailableError):
        await erase(queue)
    assert queue.fence.holders and len(queue.rows) == 1
    # Model the durable holder's server deadline having elapsed before takeover.
    queue.fence.holders.clear()
    await erase(queue)
    assert queue.rows == []


async def test_ordinary_review_keeps_its_existing_path_during_erasure(queue: Any) -> None:
    queue.rows[0]["source_kind"] = "conversation_seeder"
    queue.fence.failure = AssertionError("ordinary candidates do not use the private fence")
    result = await reject()
    assert result.new_status == "rejected" and queue.rows[-1]["proposed_value"] == VALUE
    assert queue.fence.reads == [] and queue.fence.holders == {}


async def test_private_review_cannot_replace_a_missing_original_proposal_time(queue: Any) -> None:
    queue.rows[0]["proposed_at"] = None
    with pytest.raises(ValidationError, match="proposal time"):
        await reject()
    assert len(queue.rows) == 1


@pytest.mark.parametrize(
    ("scope_type", "scope_value", "blocked"),
    [
        ("user_id", "someone", True),
        ("namespace_list", '["n", "other"]', True),
        ("namespace_list", "malformed", True),
        ("namespace_list", '["other"]', False),
        ("unknown", "", True),
    ],
)
async def test_private_evidence_checks_erasure_reach_conservatively(
    queue: Any, scope_type: str, scope_value: str, blocked: bool
) -> None:
    queue.fence.erasures = [{"scope_type": scope_type, "scope_value": scope_value}]
    if blocked:
        with pytest.raises(ValidationError, match="eras"):
            await reject()
        assert len(queue.rows) == 1
    else:
        await reject()
        assert queue.rows[-1]["status"] == "rejected"


async def test_namespace_list_eraser_keeps_the_fence_until_deletion_settles(
    queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, resume = asyncio.Event(), asyncio.Event()

    async def delete(*args: Any, **kw: Any) -> int:
        assert args[3] == {"ns": ["n", "other"]}
        entered.set()
        await resume.wait()
        queue.rows.clear()
        return 1

    monkeypatch.setattr(steps_ledgers, "_delete_many", delete)
    eraser = asyncio.create_task(
        steps_ledgers.delete_learnings_queue_by_namespace("o", ["n", "other"])
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(BackendUnavailableError):
            await reject()
    finally:
        resume.set()
        assert await eraser == 1
    assert queue.rows == [] and queue.fence.holders == {}


@pytest.mark.parametrize("kind", ["query_history", "knowledge_gap"])
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_historical_redaction_cannot_finish_before_late_status_send(
    queue: Any, monkeypatch: pytest.MonkeyPatch, kind: str, failure: type[BaseException]
) -> None:
    from server.core.hashing import served_content_sha256

    queue.rows[0]["source_kind"] = kind
    queue.rows[0]["status"] = "promoted"
    late = {**queue.rows[0], "status": "rejected", "version": 2}
    queue.failure = failure()
    with pytest.raises(failure):
        await reject()
    target = SimpleNamespace(
        agent="-",
        user_id="-",
        session_id="-",
        memory_key="definition/c",
        namespace="n",
        content_sha256=served_content_sha256(json.loads(VALUE)),
        tags=(),
    )

    async def page(org: str, namespace: str, after: str) -> Any:
        return [] if after else [dict(queue.rows[-1])]

    async def scrub(*args: Any) -> None:
        for row in queue.rows:
            row["proposed_value"] = ""

    monkeypatch.setattr(learnings.learnings_repo, "private_candidates_after", page)
    monkeypatch.setattr(learnings.learnings_repo, "scrub_private_candidate", scrub)
    with pytest.raises(BackendUnavailableError):
        await definition_review.redact_candidates("o", target, None)
    queue.rows.append(late)  # an admitted server insert settles before lease takeover
    queue.fence.holders.clear()
    await definition_review.redact_candidates("o", target, None)
    assert all(row["proposed_value"] == "" for row in queue.rows)


async def test_historical_scrub_holds_writer_fence_until_all_versions_settle(
    queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.hashing import served_content_sha256

    entered, resume = asyncio.Event(), asyncio.Event()
    target = SimpleNamespace(
        agent="-",
        user_id="-",
        session_id="-",
        memory_key="definition/c",
        namespace="n",
        content_sha256=served_content_sha256(json.loads(VALUE)),
        tags=(),
    )

    async def page(org: str, namespace: str, after: str) -> Any:
        return [] if after else [dict(queue.rows[-1])]

    async def scrub(*args: Any) -> None:
        entered.set()
        await resume.wait()
        for row in queue.rows:
            row["proposed_value"] = ""

    monkeypatch.setattr(learnings.learnings_repo, "private_candidates_after", page)
    monkeypatch.setattr(learnings.learnings_repo, "scrub_private_candidate", scrub)
    redaction = asyncio.create_task(definition_review.redact_candidates("o", target, None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(BackendUnavailableError):
            await reject()
    finally:
        resume.set()
        await redaction
    assert all(row["proposed_value"] == "" for row in queue.rows)
