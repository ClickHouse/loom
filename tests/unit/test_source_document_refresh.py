"""An open document retains only its exact, currently admitted identity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app import source_documents as app
from server.knowledge.app.source_access import Admitted


@pytest.fixture
def world(monkeypatch):
    candidate = {"namespace": "acl-" + "1" * 24, "source_uri": "file#source"}
    admitted = Admitted("team", candidate, {"version": 10, "revision": 0})
    row = SimpleNamespace(org="acme", **candidate, version=10, revision=0)
    checked = app.CheckedDocument(admitted, row)
    plan = SimpleNamespace(org="acme", failed=False, unread=False, current=lambda _item: True)
    refreshed = Admitted("team", dict(candidate), {"version": 10, "revision": 0})
    plan.check = AsyncMock(return_value=[refreshed])
    monkeypatch.setattr(
        app.store, "contents", AsyncMock(side_effect=AssertionError("No new text read"))
    )
    return plan, checked, refreshed


async def test_refresh_uses_current_proof_without_reading_text_again(world):
    plan, checked, refreshed = world
    result = await app.refresh(plan, checked)
    assert result.row is checked.row and result.admitted is refreshed
    plan.check.assert_awaited_once_with([checked.admitted.candidate])
    app.store.contents.assert_not_awaited()


@pytest.mark.parametrize("state", ["failed", "unread"])
async def test_unavailable_plan_never_revalidates_or_reads_content(world, state):
    plan, checked, _refreshed = world
    setattr(plan, state, True)
    assert await app.refresh(plan, checked) is None
    plan.check.assert_not_awaited()
    app.store.contents.assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["namespace", "source_uri", "version", "revision", "denied", "expired", "duplicate"]
)
async def test_changed_or_missing_proof_cannot_authorize_the_open_copy(world, change):
    plan, checked, refreshed = world
    if change in {"namespace", "source_uri"}:
        refreshed.candidate[change] = "other"
    elif change in {"version", "revision"}:
        refreshed.document[change] += 1
    elif change == "expired":
        plan.current = lambda _item: False
    else:
        plan.check.return_value = [] if change == "denied" else [refreshed, refreshed]
    assert await app.refresh(plan, checked) is None
    assert not plan.unread
    app.store.contents.assert_not_awaited()


async def test_conditional_refresh_exception_is_failed_without_unread(world):
    plan, checked, _refreshed = world
    plan.check.side_effect = RuntimeError("private backend detail")
    assert await app.refresh(plan, checked) is None
    assert plan.failed and not plan.unread


async def test_refresh_cancellation_propagates(world):
    plan, checked, _refreshed = world
    plan.check.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await app.refresh(plan, checked)
    assert not plan.failed and not plan.unread
