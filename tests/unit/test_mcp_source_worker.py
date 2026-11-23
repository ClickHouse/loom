"""The shared worker routes selected MCP sources through protected execution."""

import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.code.app.index.pass_budget import PassBudget
from server.knowledge.app import mcp_connections
from server.knowledge.app import mcp_source_worker as worker
from server.knowledge.app import source_sync as sync
from server.knowledge.app import source_sync_mcp as engine
from server.knowledge.app.mcp_contracts import encode_source
from server.knowledge.app.mcp_profiles import MCPProfileCatalog
from server.knowledge.domain.sources import mirror_synced, served
from server.knowledge.store.mcp_source_schedule import Header
from tests.unit.test_mcp_source_sync import MIRROR, RESOURCE
from tests.unit.test_mcp_source_sync import harness as harness
from tests.unit.test_mcp_source_sync import storage as storage
from tests.unit.test_mcp_source_sync import upstream as upstream


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_bounded_worker_lazily_runs_the_leased_engine_and_rejects_stale_profiles(
    storage, upstream, monkeypatch, changed
):
    h = storage
    header = Header(
        h.source.org,
        h.source.namespace,
        h.source.name,
        h.source.source_id,
        h.contract.profile_id,
        "0" * 64 if changed else h.contract.profile_sha256,
        "A" * 64,
    )
    pages = []
    progress = {}

    async def page(after, limit, *, query_limits):
        assert limit <= sync.settings.knowledge_source_mcp_schedule_page_size
        assert query_limits == worker.query_limits()
        pages.append(after)
        return [] if after else [header]

    async def resume(name, **kwargs):
        assert name == worker.scheduler.PROGRESS_WORKER
        return progress.get("cursor", "")

    async def record(name, **kwargs):
        assert name == worker.scheduler.PROGRESS_WORKER and kwargs["namespace"] == ""
        progress["cursor"] = kwargs["resume_cursor"]
        return SimpleNamespace()

    @asynccontextmanager
    async def lease(name, **kwargs):
        assert name == sync.turn_lease_name(h.source.org, h.source.namespace, h.source.name)
        yield SimpleNamespace()

    monkeypatch.setattr(type(sync.settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(
        worker, "load_mcp_profile_catalog", mcp_connections.load_mcp_profile_catalog
    )
    monkeypatch.setattr(worker.headers, "candidate_page", page)
    monkeypatch.setattr(worker.scheduler.status_store, "resume_cursor", resume)
    monkeypatch.setattr(worker.scheduler.status_store, "record_manual_pass", record)
    monkeypatch.setattr(sync.store, "inactive_namespaces", AsyncMock(return_value=set()))
    monkeypatch.setattr(sync.store, "read_states", AsyncMock(return_value={}))
    monkeypatch.setattr(sync, "require_source_registry", AsyncMock())
    monkeypatch.setattr(sync.leases, "cluster_lease", lease)
    monkeypatch.setattr(sync, "_write_state", AsyncMock())
    started = time.monotonic()
    result = sync.PassResult()
    await worker.run(PassBudget(started, started + 15), result)
    assert pages == [""] and progress["cursor"].startswith("mcp1:")
    assert result.sources == int(not changed)
    assert bool(h.reads) == bool(h.doc_rows) == bool(h.originals) == (not changed)
    assert result.private_work == (not changed)


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


@pytest.mark.asyncio
async def test_failed_mcp_catalog_keeps_the_ordinary_recording_lane(harness, monkeypatch):
    recording = replace(
        harness.source,
        kind="recording",
        credential_mode="shared",
        access="namespace",
        source_contract_version=0,
        generic_config_json="",
        generic_items_json="",
        config={"provider": "zoom", "ids": ["123456789"]},
    )
    monkeypatch.setattr(type(sync.settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(sync.settings, "knowledge_mirrored_sources_enabled", True)
    monkeypatch.setattr(worker, "run", AsyncMock(side_effect=ValueError("private config")))
    monkeypatch.setattr(sync.store, "all_active_sources", AsyncMock(return_value=[recording]))
    monkeypatch.setattr(sync, "_retire_removed_sources", AsyncMock())
    monkeypatch.setattr(sync, "require_source_registry", AsyncMock())
    monkeypatch.setattr(sync, "_usable", AsyncMock(return_value=[recording]))
    monkeypatch.setattr(sync, "_states", AsyncMock(return_value={}))
    turn = AsyncMock(return_value=sync.TurnResult())
    monkeypatch.setattr(sync, "_sync_one", turn)
    monkeypatch.setattr(sync, "_extract_pending", AsyncMock())
    monkeypatch.setattr(sync.pass_context, "resume_cursor", lambda: "")
    monkeypatch.setattr(sync.pass_context, "set_cursor", lambda value: None)
    monkeypatch.setattr(sync.pass_context, "note_failure", lambda *args: None)
    result = await sync.run_sync()
    assert turn.await_count == 1 and turn.await_args.args[0] == recording
    assert result.private_work and result.failed == 1 and result.sources == 1
    assert result.errors == ["MCP_SCHEDULER_UNAVAILABLE"]


@pytest.mark.asyncio
async def test_unconfigured_mcp_lane_opens_no_scheduler_or_store(monkeypatch):
    monkeypatch.setattr(worker, "load_mcp_profile_catalog", lambda: MCPProfileCatalog())
    dispatch = AsyncMock(side_effect=AssertionError("unconfigured lane opened storage"))
    monkeypatch.setattr(worker.scheduler, "run_pass", dispatch)
    started = time.monotonic()
    result = sync.PassResult()
    await worker.run(PassBudget(started, started + 1), result)
    assert result == sync.PassResult()
    dispatch.assert_not_awaited()
