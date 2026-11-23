"""Generic MCP turns keep private sync state under the source's immutable ID."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import source_sync as sync
from tests.unit.test_mcp_source_sync import MIRROR
from tests.unit.test_mcp_source_sync import harness as harness

pytestmark = pytest.mark.asyncio


async def test_a_mirrored_source_s_namespace_row_holds_only_its_codes(harness, monkeypatch):
    src = replace(harness.source, mirror=MIRROR)
    read = {
        "state_value": "",
        "detail": json.dumps(
            {
                "requested_at": "request",
                "full": True,
                "file_waits": {"private-id": ["r", 1, "x"]},
                "error": "private detail",
            }
        ),
    }
    monkeypatch.setattr(sync.store, "read_states", AsyncMock(return_value={src.name: read}))
    writes = AsyncMock()
    monkeypatch.setattr(sync.store, "write_state", writes)
    turn = sync.TurnResult(changed=2, mirror=MIRROR)
    turn.error = "private permission failure"
    async with accounts.account_lease(src.org, src.owner_subject, src.connection_slot):
        await sync._write_state(src, read, turn)
    assert writes.await_count == 2
    private, public = writes.await_args_list
    assert private.args == (src.org, MIRROR, src.source_id)
    assert public.args == (src.org, src.namespace, src.name)
    assert private.kwargs["detail"]["changed"] == 2
    assert private.kwargs["detail"]["error"] == "private permission failure"
    assert set(public.kwargs["detail"]) == {"last_synced_at", "status"}
    assert not any(
        secret in json.dumps(public.kwargs)
        for secret in ("private-id", "private detail", "private permission failure")
    )
    assert harness.locks == []


async def test_a_source_added_again_under_the_name_inherits_no_state(harness, monkeypatch):
    old = harness.source.source_id
    src = replace(harness.source, source_id="22222222-2222-4222-8222-222222222222", mirror=MIRROR)
    harness.source = src
    monkeypatch.setattr(sync.store, "read_states", AsyncMock(return_value={}))
    reads = AsyncMock(return_value={})
    monkeypatch.setattr(sync, "mirror_state", reads)
    writes = AsyncMock()
    monkeypatch.setattr(sync.store, "write_state", writes)
    async with accounts.account_lease(src.org, src.owner_subject, src.connection_slot):
        await sync._write_state(src, {}, sync.TurnResult(mirror=MIRROR))
    reads.assert_awaited_once_with(src, MIRROR)
    assert writes.await_args_list[0].args == (src.org, MIRROR, src.source_id)
    assert all(old not in call.args for call in writes.await_args_list)
    assert all("file_waits" not in call.kwargs["detail"] for call in writes.await_args_list)


async def test_a_turn_that_fails_before_it_knows_its_mirror_leaves_the_mirror_s_state(
    harness, monkeypatch
):
    src = harness.source
    read = {"state_value": "", "detail": json.dumps({"requested_at": "request", "full": True})}
    monkeypatch.setattr(sync.store, "read_states", AsyncMock(return_value={src.name: read}))
    reads = AsyncMock(side_effect=AssertionError("an unknown mirror must not be read"))
    monkeypatch.setattr(sync, "mirror_state", reads)
    writes = AsyncMock()
    monkeypatch.setattr(sync.store, "write_state", writes)
    turn = sync.TurnResult()
    turn.fail("unavailable", "private transport error")
    async with accounts.account_lease(src.org, src.owner_subject, src.connection_slot):
        await sync._write_state(src, read, turn)
    reads.assert_not_awaited()
    writes.assert_awaited_once()
    call = writes.await_args
    assert call.args == (src.org, src.namespace, src.name)
    assert call.kwargs["detail"]["status"] == "unavailable"
    assert {k: call.kwargs["detail"][k] for k in ("requested_at", "full")} == {
        "requested_at": "request",
        "full": True,
    }
    assert "private transport error" not in json.dumps(call.kwargs)
