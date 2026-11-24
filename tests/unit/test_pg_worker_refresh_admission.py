import json
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from server.knowledge.domain.mcp_worker_refresh import RefreshRejected, RefreshScope
from server.knowledge.store import pg_worker_refresh_admission as admission

_SOURCE = admission.AdmissionSource("org", "team", UUID(int=5), "agent", "alice", "-", "alice")
_ASSIGNMENT = admission.QueueAssignment(
    UUID(int=6),
    RefreshScope(
        "org", "team", "alice", str(UUID(int=5)), "connection", "copy", 2, 3, 4, 5, "a" * 64
    ),
)


@pytest.fixture
def connection(monkeypatch):
    monkeypatch.setattr(admission.pg, "enabled", lambda: True)
    return Mock(
        is_in_transaction=Mock(return_value=True),
        execute=AsyncMock(),
        fetchval=AsyncMock(return_value="alice"),
        fetch=AsyncMock(return_value=[]),
    )


def _terminal(count, *, expire=0):
    return [
        {"item_key": f"item-{index}", "state": "completed", "expired": index < expire}
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_pending_or_claimed_batch_stops_admission_even_if_old(connection):
    connection.fetch.return_value = [
        {"item_key": "old", "state": "claimed", "expired": True, "current_assignment": True}
    ]
    assert not await admission.reserve_on(connection, _SOURCE, _ASSIGNMENT, 1)
    assert connection.execute.await_count == 2  # Namespace lock and bounded authority row.


@pytest.mark.asyncio
async def test_admission_trims_only_expired_terminal_rows_of_the_exact_source(connection):
    connection.fetch.return_value = _terminal(128, expire=64)
    assert await admission.reserve_on(connection, _SOURCE, _ASSIGNMENT, 64)
    query, *parameters = connection.execute.await_args.args
    assert "state IN ('completed', 'cancelled', 'exhausted')" in query
    assert parameters == ["org", "team", UUID(int=5), [f"item-{index}" for index in range(64)]]
    assert connection.fetch.await_args.args[-4] == 129


@pytest.mark.asyncio
async def test_no_capacity_does_not_prune_live_receipts_or_partially_admit(connection):
    connection.fetch.return_value = _terminal(128, expire=63)
    assert not await admission.reserve_on(connection, _SOURCE, _ASSIGNMENT, 64)
    assert connection.execute.await_count == 2


@pytest.mark.asyncio
async def test_admission_refuses_unexpected_residue_above_bound(connection):
    connection.fetch.return_value = _terminal(129, expire=129)
    with pytest.raises(RefreshRejected, match="invariant"):
        await admission.reserve_on(connection, _SOURCE, _ASSIGNMENT, 1)
    assert connection.execute.await_count == 2


@pytest.mark.asyncio
async def test_admission_refuses_different_current_owner_before_loading_rows(connection):
    connection.fetchval.return_value = "bob"
    with pytest.raises(RefreshRejected, match="authority changed"):
        await admission.reserve_on(connection, _SOURCE, _ASSIGNMENT, 1)
    connection.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_obsolete_claim_is_fenced_without_pruning_unexpired_receipts(connection):
    connection.fetch.return_value = [
        {"item_key": "old", "state": "claimed", "expired": True, "current_assignment": False},
        *_terminal(1),
    ]
    assert await admission.reserve_on(connection, _SOURCE, _ASSIGNMENT, 64)
    retire, org, namespace, source, candidates = connection.execute.await_args.args
    assert "fence = fence + 1" in retire
    assert (org, namespace, source, candidates) == ("org", "team", _SOURCE.source_id, ["old"])
    assert connection.execute.await_count == 3
    scope_filter = json.loads(connection.fetch.await_args.args[-1])
    assert "actor" not in scope_filter
    assert scope_filter["source_generation"] == 3
    assert scope_filter["destination_id"] == "copy"
