"""The shared worker routes selected MCP sources through protected execution."""

import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.code.app.index.pass_budget import PassBudget
from server.knowledge.app import source_sync as sync
from server.knowledge.app import source_sync_mcp as engine
from server.knowledge.app.mcp_contracts import encode_source
from server.knowledge.domain.sources import mirror_synced, served
from tests.unit.test_mcp_source_sync import MIRROR, RESOURCE
from tests.unit.test_mcp_source_sync import harness as harness
from tests.unit.test_mcp_source_sync import storage as storage
from tests.unit.test_mcp_source_sync import upstream as upstream


@pytest.mark.asyncio
async def test_shared_worker_runs_actual_mcp_engine_as_private_work(storage, upstream, monkeypatch):
    h = storage

    @asynccontextmanager
    async def lease(name, **kwargs):
        assert name == sync.turn_lease_name(h.source.org, h.source.namespace, h.source.name)
        yield SimpleNamespace()

    monkeypatch.setattr(sync.leases, "cluster_lease", lease)
    recorded = AsyncMock()
    monkeypatch.setattr(sync, "_write_state", recorded)
    assert mirror_synced(h.source) and not served(h.source)
    assert sync._turn_of(h.source) is engine.turn
    result = sync.PassResult()
    started = time.monotonic()
    turn = await sync._sync_one(
        h.source, {}, sync.Pass(PassBudget(started, started + 15), 10), result
    )
    assert turn is not None and turn.complete and turn.changed == 1
    assert result.private_work and not result.errors
    assert h.reads == [RESOURCE] and h.doc_rows and h.chunk_rows and h.originals
    recorded.assert_awaited_once_with(h.source, {}, turn)


@pytest.mark.asyncio
async def test_only_supported_unpaused_indexed_mcp_selections_are_worker_candidates(
    harness, monkeypatch
):
    h = harness
    monkeypatch.setattr(type(sync.settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(sync.store, "inactive_namespaces", AsyncMock(return_value=set()))
    assert await sync._usable([h.source]) == [h.source]
    assert not served(h.source)
    for mode, paused in (("live", False), ("snapshot", True), ("selected_refresh", True)):
        contract = h.contract.model_copy(update={"mode": mode, "paused": paused})
        config, items = encode_source(contract)
        candidate = replace(h.source, generic_config_json=config, generic_items_json=items)
        assert await sync._usable([candidate]) == []
    for change in (
        {"source_contract_version": 2},
        {"credential_mode": "shared"},
        {"access": "namespace"},
        {"generic_config_json": "invalid"},
    ):
        assert await sync._usable([replace(h.source, **change)]) == []


@pytest.mark.asyncio
async def test_worker_state_retains_request_scope_without_publishing_private_progress(
    harness, monkeypatch
):
    h = harness
    writes = []

    async def write(org, namespace, key, *, state_value, detail):
        writes.append((org, namespace, key, state_value, detail))

    monkeypatch.setattr(sync.store, "write_state", write)
    current = {
        "detail": json.dumps(
            {
                "requested_at": "request",
                "full": True,
                "mcp_selection": "scope",
                "private": "must stay private",
            }
        )
    }
    turn = sync.TurnResult(
        complete=False,
        mirror=MIRROR,
        kind_state={"mcp_next_index": 1, "mcp_scope_sha256": "a" * 64},
    )
    await sync._write_mirrored_state(h.source, current, turn, current, {})
    assert len(writes) == 2
    private, public = writes
    assert private[1:3] == (MIRROR, h.source.source_id)
    assert private[4]["mcp_next_index"] == 1
    assert public[1:3] == (h.source.namespace, h.source.name)
    assert public[4]["mcp_selection"] == "scope"
    assert public[4]["requested_at"] == "request"
    assert set(public[4]) == {"mcp_selection", "requested_at", "full", "last_synced_at", "status"}
