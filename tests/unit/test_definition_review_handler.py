"""Private review writes only an approved draft and preserves another writer's edits."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.core.errors import DefinitionPromotionRefusedError, PreconditionFailedError
from server.memory.api.models_learnings import LearningsReviewInput
from server.memory.app import definition_review, learnings, promote, write
from server.memory.store.rows import MemoryRow
from tests.unit.definition_queue_fake import install

VALUE = {"term": "ARR", "aliases": [], "body": "Annual revenue."}
KEY = "definition/draft"


@pytest.fixture
def review(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fence = install(monkeypatch)
    state: dict[str, Any] = {
        "fence": fence,
        "candidate": {
            "candidate_id": "c",
            "memory_key": KEY,
            "memory_type": "definition",
            "proposed_value": json.dumps(VALUE),
            "source_kind": "query_history",
            "status": "pending",
            "version": 10**19,
            "proposed_at": datetime(2026, 1, 1),
            "supporters": [],
            "rationale": "SQL",
        },
        "head": None,
        "copy": None,
        "shared": [],
        "writes": [],
        "promotions": [],
        "decisions": [],
    }

    async def find(*a: Any, **kw: Any) -> Any:
        return state["candidate"]

    async def head(*a: Any, **kw: Any) -> Any:
        assert kw.get("strong") and kw["scope"].user_id is None
        return state["head"]

    async def copy(*a: Any, **kw: Any) -> Any:
        return state["copy"]

    async def shared(*a: Any, **kw: Any) -> Any:
        return state["shared"]

    async def put(input: Any) -> None:
        state["writes"].append(input)

    async def publish(input: Any, **kw: Any) -> Any:
        assert kw["reviewed_candidate_id"] == "c"
        state["promotions"].append(input)
        if state.get("promotion_error"):
            raise RuntimeError("promotion unavailable")
        from types import SimpleNamespace

        return SimpleNamespace(promotion_event="promotion-id")

    async def decide(row: Any) -> None:
        state["decisions"].append(row)

    async def yes(*a: Any, **kw: Any) -> bool:
        return True

    async def none(*a: Any, **kw: Any) -> None:
        return None

    async def grade(*a: Any, **kw: Any) -> str:
        return ""

    @asynccontextmanager
    async def claim(*a: Any, **kw: Any) -> Any:
        yield True

    from server.core import leases
    from server.observability import audit

    monkeypatch.setattr(audit, "emit", none)
    monkeypatch.setattr(leases, "holding", claim)
    monkeypatch.setattr(learnings.learnings_repo, "find_candidate", find)
    monkeypatch.setattr(learnings.learnings_repo, "insert_row", decide)
    monkeypatch.setattr(learnings, "private_candidates_visible", yes)
    monkeypatch.setattr(learnings, "writer_actor", lambda _: "user:reviewer")
    monkeypatch.setattr(definition_review, "require_draft_fleet", none)
    monkeypatch.setattr(definition_review, "detect_level", grade)
    monkeypatch.setattr(definition_review.memory_repo, "get_by_key", head)
    monkeypatch.setattr(definition_review.shared_repo, "fetch_shared", copy)
    monkeypatch.setattr(definition_review.shared_repo, "shared_definitions", shared)
    monkeypatch.setattr(promote, "_check_shared_definitions", none)
    monkeypatch.setattr(promote, "handle_memory_promote", publish)
    monkeypatch.setattr(write, "handle_memory_set", put)
    return state


def request(**kw: Any) -> LearningsReviewInput:
    return LearningsReviewInput(org="o", namespace="n", candidate_id="c", decision="promote", **kw)


async def test_new_term_is_created_conditionally_and_decision_is_token_stamped(review: Any) -> None:
    result = await learnings.handle_learnings_review(request(approver="forged"))
    assert result.promoted_to_shared
    source = review["writes"][0]
    assert source.if_match_sha256 == "absent"
    assert source.value == VALUE and source.clear_expiry and source.source_class == "derived"
    assert (source.agent, source.user_id, source.session_id) == ("-", "-", "-")
    decision = review["decisions"][0]
    assert decision["decided_by"] == "user:reviewer" and decision["version"] > 10**19


@pytest.mark.parametrize("decision", ["promote", "reject"])
@pytest.mark.parametrize("status", ["pending", "promoted", "rejected"])
async def test_redacted_candidate_is_missing_before_status_handling(
    review: Any, decision: str, status: str
) -> None:
    review["candidate"].update(proposed_value="", status=status)
    with pytest.raises(HTTPException) as exc:
        await learnings.handle_learnings_review(request().model_copy(update={"decision": decision}))
    assert exc.value.status_code == 404
    assert not review["writes"] and not review["decisions"]


async def test_failed_promotion_keeps_draft_and_candidate_pending(review: Any) -> None:
    review["promotion_error"] = True
    with pytest.raises(RuntimeError, match="promotion unavailable"):
        await learnings.handle_learnings_review(request())
    assert len(review["writes"]) == 1
    assert not review["decisions"]
    assert review["candidate"]["status"] == "pending"


def head(value: Any) -> MemoryRow:
    return MemoryRow(
        memory_id="00000000-0000-0000-0000-000000000001",
        user_id="-",
        session_id="-",
        pinned=False,
        expires_at=None,
        org="o",
        namespace="n",
        agent="-",
        memory_key=KEY,
        memory_type="definition",
        value=value,
        confidence=1,
        created_at=datetime(2026, 1, 1),
        version=99,
    )


async def test_concurrent_rename_does_not_replace_new_term_at_selected_key(review: Any) -> None:
    renamed = {**VALUE, "term": "MRR"}
    review.update(
        shared=[{"memory_key": KEY, "term": "ARR", "aliases": []}],
        copy={"value": json.dumps(renamed), "tombstoned_at": None},
        head=head(renamed),
    )
    with pytest.raises(PreconditionFailedError):
        await learnings.handle_learnings_review(request())
    assert not review["writes"] and not review["promotions"]


async def test_unpromoted_writer_edit_is_preserved(review: Any) -> None:
    review["head"] = head({**VALUE, "body": "A writer's own revision."})
    with pytest.raises(DefinitionPromotionRefusedError):
        await learnings.handle_learnings_review(request())
    assert not review["writes"] and not review["promotions"]


async def test_exact_draft_retry_does_not_rewrite_source(review: Any) -> None:
    review["head"] = head(VALUE)
    await learnings.handle_learnings_review(request())
    assert not review["writes"] and len(review["promotions"]) == 1


@pytest.mark.parametrize("kind", ["query_history", "knowledge_gap"])
async def test_completed_erasure_after_candidate_read_prevents_all_publication(
    review: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from server.core.errors import ValidationError
    from server.erasure.store import steps_ledgers

    review["candidate"]["source_kind"] = kind
    find = learnings.learnings_repo.find_candidate
    reads = 0

    async def delete(*args: Any, **kw: Any) -> int:
        review["candidate"] = None
        review["decisions"].clear()
        return 1

    async def stale(*args: Any, **kw: Any) -> Any:
        nonlocal reads
        row = await find(*args, **kw)
        reads += 1
        if reads == 2:
            row = dict(row)
            review["fence"].erasures = [{"scope_type": "namespace", "scope_value": "n"}]
            await steps_ledgers.delete_learnings_queue_by_namespace("o", "n")
        return row

    monkeypatch.setattr(steps_ledgers, "_delete_many", delete)
    monkeypatch.setattr(learnings.learnings_repo, "find_candidate", stale)
    with pytest.raises(ValidationError, match="eras"):
        await learnings.handle_learnings_review(request())
    assert not review["writes"] and not review["promotions"] and not review["decisions"]
    assert review["candidate"] is None


@pytest.mark.parametrize("boundary", ["source", "shared"])
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_uncertain_private_publication_holds_namespace_eraser_out(
    review: Any, monkeypatch: pytest.MonkeyPatch, boundary: str, failure: type[BaseException]
) -> None:
    from unittest.mock import AsyncMock

    from server.core.errors import BackendUnavailableError
    from server.erasure.store import steps_ledgers

    module, name = (
        (write, "handle_memory_set") if boundary == "source" else (promote, "handle_memory_promote")
    )
    monkeypatch.setattr(module, name, AsyncMock(side_effect=failure()))
    deleted = AsyncMock(return_value=1)
    monkeypatch.setattr(steps_ledgers, "_delete_many", deleted)
    with pytest.raises(failure):
        await learnings.handle_learnings_review(request())
    with pytest.raises(BackendUnavailableError):
        await steps_ledgers.delete_learnings_queue_by_namespace("o", "n")
    deleted.assert_not_awaited()
    assert not review["decisions"]


@pytest.mark.parametrize("boundary", ["source", "shared"])
async def test_namespace_erasure_cannot_finish_during_private_publication(
    review: Any, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from server.core.errors import BackendUnavailableError
    from server.erasure.store import steps_ledgers

    entered, resume = asyncio.Event(), asyncio.Event()
    module, name = (
        (write, "handle_memory_set") if boundary == "source" else (promote, "handle_memory_promote")
    )
    publish = getattr(module, name)

    async def paused(*args: Any, **kw: Any) -> Any:
        entered.set()
        await resume.wait()
        return await publish(*args, **kw)

    deletions = []

    async def delete(*args: Any, **kw: Any) -> int:
        deletions.append(True)
        review["candidate"] = None
        review["decisions"].clear()
        return 1

    monkeypatch.setattr(module, name, paused)
    monkeypatch.setattr(steps_ledgers, "_delete_many", delete)
    writer = asyncio.create_task(learnings.handle_learnings_review(request()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(BackendUnavailableError):
            await steps_ledgers.delete_learnings_queue_by_namespace("o", "n")
        assert not deletions
    finally:
        resume.set()
        result = await writer
    assert result.promoted_to_shared and review["decisions"]
    await steps_ledgers.delete_learnings_queue_by_namespace("o", "n")
    assert deletions == [True] and not review["decisions"]
