import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from server.core.ch import query_settings
from server.core.errors import OverloadedError
from server.erasure.app.connected_accounts import require_restore_erased, require_sources_erased
from server.knowledge.app.connected_restore import _metadata
from server.knowledge.domain.sources import mirror_synced, served
from server.knowledge.store import sources
from tests.unit.ch_fake import RecordingCH

CONNECTION = UUID("abcd0000-1234-4abc-8000-0000ffffffff")
SOURCE = UUID("dcba0000-1234-4abc-8000-0000ffffffff")
SLOT = f"mcp:{CONNECTION.hex}"
RAW_CONFIG = ' {"opaque-config": '
RAW_ITEMS = '[{"opaque-items": 1}, '


def _row() -> dict[str, Any]:
    return {
        "org": "tenant",
        "namespace": "docs",
        "name": "selected",
        "source_kind": "mcp",
        "source_id": SOURCE,
        "credential_mode": "worker",
        "owner_subject": "reader",
        "connection_id": str(CONNECTION),
        "active": False,
    }


async def test_worker_metadata_roundtrip_keeps_raw_payload_and_stays_inactive():
    row = dict(_row(), config_json=RAW_CONFIG, items_json=RAW_ITEMS)
    ch = RecordingCH().on("loom.knowledge_sources FINAL", [row])
    source = await sources.latest_source("tenant", "docs", "selected", ch=ch)
    assert source is not None
    assert source.config == {} and source.items == []
    assert "opaque-" not in repr(source)
    assert not served(source) and not mirror_synced(source)

    await sources.insert_source(source, ch=ch)
    [written] = ch.calls[-1].rows
    for name in ("config_json", "items_json", "credential_mode", "connection_id", "owner_subject"):
        assert written[name] == row[name]
    assert written["source_id"] == str(SOURCE)
    assert not written["active"] and not written["kind"]

    with pytest.raises(ValueError, match="Only inactive worker source history"):
        await sources.insert_source(replace(source, active=True), ch=ch)
    assert [call.kind for call in ch.calls] == ["query_one", "insert"]


async def test_active_worker_row_keeps_selection_for_protected_readers_only():
    registration = {"worker_registration": '{"version":2}'}
    items = [{"id": "urn:doc:selected", "type": "resource"}]
    row = dict(
        _row(),
        active=True,
        access="mirrored",
        config_json=json.dumps(registration),
        items_json=json.dumps(items),
    )
    ch = RecordingCH().on("loom.knowledge_sources FINAL", [row])
    source = await sources.get_source("tenant", "docs", "selected", ch=ch)
    assert source is not None
    assert source.config == registration and source.items == items
    assert source.worker_history is not None
    assert source.worker_history.config_json == row["config_json"]
    assert source.worker_history.items_json == row["items_json"]
    assert not served(source) and not mirror_synced(source)


@pytest.mark.parametrize("raw_items", ['[{"id":"urn:doc:selected"}, null]', "{}", "["])
async def test_active_worker_does_not_repair_a_damaged_selection(raw_items):
    row = dict(_row(), active=True, access="mirrored", config_json="{}", items_json=raw_items)
    ch = RecordingCH().on("loom.knowledge_sources FINAL", [row])
    source = await sources.get_source("tenant", "docs", "selected", ch=ch)
    assert source is not None
    assert source.items == []
    assert source.worker_history is not None and source.worker_history.items_json == raw_items


async def test_unreadable_worker_config_does_not_block_native_sync_inventory():
    raw_config = '{"child":' * 10_000 + "{}" + "}" * 10_000
    worker_row = dict(_row(), active=True, access="mirrored", config_json=raw_config)
    native_row = dict(
        _row(), name="native", source_kind="gdrive", credential_mode="shared", active=True
    )
    ch = RecordingCH().on("loom.knowledge_sources FINAL", [worker_row, native_row])

    worker, native = await sources.all_active_sources(ch=ch)

    assert worker.config == {} and not served(worker) and not mirror_synced(worker)
    assert worker.worker_history is not None and worker.worker_history.config_json == raw_config
    assert native.name == "native" and served(native)


async def test_inactive_worker_rewrite_keeps_the_legacy_only_marker():
    row = dict(
        _row(),
        kind="mcp",
        source_kind="notion",
        credential_mode="connected",
        config_json=RAW_CONFIG,
        items_json=RAW_ITEMS,
    )
    ch = RecordingCH().on("loom.knowledge_sources FINAL", [row])
    source = await sources.latest_source("tenant", "docs", "selected", ch=ch)
    assert source is not None and not served(source) and not mirror_synced(source)
    await sources.insert_source(source, ch=ch)
    written = ch.calls[-1].rows[0]
    for field in ("kind", "source_kind", "credential_mode", "config_json", "items_json"):
        assert written[field] == row[field]


@pytest.mark.parametrize(("kind", "mode"), [("mcp", "shared"), ("gdrive", "worker")])
async def test_partial_worker_marker_is_never_native(kind, mode):
    row = dict(
        _row(),
        source_kind=kind,
        credential_mode=mode,
        active=True,
        access="mirrored",
        config_json='{"worker_registration":"not a current binding"}',
        items_json='[{"id":"urn:doc:unsafely-marked"}]',
    )
    ch = RecordingCH().on("loom.knowledge_sources FINAL", [row])
    source = await sources.latest_source("tenant", "docs", "selected", ch=ch)
    assert source is not None
    assert source.config == {} and source.items == []
    assert not served(replace(source, access="namespace"))
    assert not mirror_synced(replace(source, access="mirrored"))


async def test_slot_history_and_erasure_bind_same_owner_and_canonical_connection():
    owner = ("tenant", "reader", SLOT)
    ch = RecordingCH()
    ch.on("SELECT DISTINCT org, namespace", [_row()])
    ch.on("SELECT DISTINCT namespace", [{"namespace": "docs"}])
    ch.on("SELECT count()", [{"n": 1}], times=1)
    ch.on("SELECT count()", [{"n": 0}])
    assert await sources.connected_owner_namespaces(*owner, ch=ch) == ["docs"]
    keys, [source] = await _metadata(ch, owner=owner)
    assert keys == [(*owner, str(CONNECTION))]
    assert source.source_id == str(SOURCE)
    assert source.credential_mode == "worker"

    args = ("tenant", "docs", "reader", SLOT)
    with pytest.raises(OverloadedError, match="cleanup is incomplete"):
        await require_sources_erased(*args, ch=ch)
    await sources.erase_connected_owner(*args, ch=ch)
    await require_sources_erased(*args, ch=ch)

    orgs, history, before, delete, after = ch.calls
    assert history.params["owner"] == ("tenant", "reader", str(CONNECTION))
    for call in (orgs, before, delete, after):
        assert call.params["org"] == "tenant" and call.params["owner"] == "reader"
        assert call.params["kind"] == str(CONNECTION)
        assert "connection_id = {kind:String}" in call.sql and "FINAL" not in call.sql
    assert "source_kind = 'mcp' AND credential_mode = 'worker'" in delete.sql
    for call in (orgs, history, before, after):
        assert "(source_kind = 'mcp' OR kind = 'mcp' OR credential_mode = 'worker')" in call.sql
    assert before.params["ns"] == delete.params["ns"] == after.params["ns"] == "docs"


async def test_deployment_restore_refuses_worker_history_before_native_inventory():
    def worker_history(_sql, _params):
        return [{"present": 1}] if query_settings().get("select_sequential_consistency") else []

    ch = RecordingCH(strict=False).on("SELECT 1 AS present", worker_history)
    with pytest.raises(RuntimeError, match="worker source history requires worker-aware restore"):
        await _metadata(ch)
    [query] = ch.calls
    assert "source_kind = 'mcp' OR kind = 'mcp' OR credential_mode = 'worker'" in query.sql
    assert "LIMIT 1" in query.sql and "FINAL" not in query.sql


async def test_deployment_restore_census_refuses_worker_history_on_a_lagging_replica():
    def worker_history(_sql, _params):
        return [{"n": 1 if query_settings().get("select_sequential_consistency") else 0}]

    ch = (
        RecordingCH()
        .on("FROM loom.connected_accounts", [{"n": 0}])
        .on("FROM loom.knowledge_sources", worker_history)
    )
    with pytest.raises(OverloadedError, match="restore cleanup is incomplete"):
        await require_restore_erased(ch=ch)


@pytest.mark.parametrize("slot", ["mcp", "mcp:wrong", "mcp:" + "0" * 32])
async def test_invalid_worker_slot_never_runs_a_native_erase(slot):
    ch = RecordingCH()
    with pytest.raises(ValueError, match="Invalid worker source slot"):
        await sources.erase_connected_owner("tenant", "docs", "reader", slot, ch=ch)
    assert not ch.calls


async def test_native_connected_inventory_excludes_any_worker_marker():
    ch = RecordingCH().on("loom.knowledge_sources FINAL", [])
    assert await sources.connected_sources("tenant", "reader", ch=ch) == []
    [query] = ch.calls
    assert (
        "AND NOT (source_kind = 'mcp' OR kind = 'mcp' OR credential_mode = 'worker')" in query.sql
    )
    assert query.params["org"] == "tenant" and query.params["subject"] == "reader"
