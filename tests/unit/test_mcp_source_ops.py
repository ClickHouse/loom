"""Synthetic I/O seams exercise real source codecs, stores, locks and authority.

No database, provider or index runs here. Hosted qualification must exercise those
systems together with document publication and protected reads.
"""

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.knowledge.api.models_mcp_sources import (
    MCPSourceAddInput,
    MCPSourceInput,
    MCPSourceListInput,
    MCPSourceRemoveInput,
    MCPSourceUpdateInput,
)
from server.knowledge.app import mcp_source_ops as ops
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.knowledge.domain.mcp_sources import ConnectionEnvelope
from server.operations.base import Role

ORG = "example"
NS = "collection"
OWNER = "person"
SLOT = "mcp:" + "a" * 32
GEN = "a" * 32 + "/1" + "b" * 31
NONCE = "d290f1ee-6c54-4b01-90e6-d701748f0851"


class SourceIO:
    def __init__(self):
        self.rows = {}
        self.states = {}
        self.held = []
        self.writes = []
        self.authorized = []
        self.deny_lease = False
        self.readable = True
        self.owner = OWNER

    @asynccontextmanager
    async def lease(self, name, **kwargs):
        self.held.append(name)
        try:
            yield not self.deny_lease
        finally:
            assert self.held.pop() == name

    async def insert(self, table, rows):
        assert len(self.held) == 3
        assert self.held[0].startswith("scim-subject:")
        assert self.held[1].startswith("connected-account:")
        assert self.held[2] == "knowledge-source:example:collection"
        for row in rows:
            key = (row["org"], row["namespace"], row.get("name", row.get("state_key")))
            if table == ops.store.TABLE:
                assert row["config_json"] and row["items_json"]
                self.rows[key] = deepcopy(row)
            elif table == ops.store.STATE_TABLE:
                self.states[key] = deepcopy(row)
            else:
                raise AssertionError(table)
            self.writes.append((table, deepcopy(row)))

    async def query_one(self, sql, *, params):
        if "SELECT count() AS n" in sql:
            assert "FROM loom.documents FINAL" in sql or "FROM loom.knowledge_sources FINAL" in sql
            return {"n": 0}
        assert "FROM loom.knowledge_sources FINAL" in sql
        assert set(params) == {"org", "ns", "name"}
        row = self.rows.get((params["org"], params["ns"], params["name"]))
        if row and "AND active" in sql and not row["active"]:
            return None
        return deepcopy(row)

    async def query_all(self, sql, *, params):
        if "AND name > {after:String}" in sql:
            assert "FROM loom.knowledge_sources FINAL" in sql
            assert "namespace = {ns:String}" in sql
            assert "owner_subject = {subject:String}" in sql
            assert "source_kind = 'mcp' AND credential_mode = 'connected'" in sql
            assert set(params) == {"org", "ns", "subject", "after", "include_retired", "limit"}
            assert 1 <= params["limit"] <= 21
            rows = [
                deepcopy(r)
                for r in self.rows.values()
                if (r["org"], r["namespace"], r["owner_subject"])
                == (params["org"], params["ns"], params["subject"])
                and r["source_kind"] == "mcp"
                and r["credential_mode"] == "connected"
                and r["name"] > params["after"]
                and (params["include_retired"] or r["active"])
            ]
            return sorted(rows, key=lambda r: r["name"])[: params["limit"]]
        if "owner_subject = {subject:String}" in sql:
            assert "FROM loom.knowledge_sources FINAL" in sql
            assert set(params) == {"org", "subject", "limit"}
            return [
                deepcopy(r)
                for r in self.rows.values()
                if r["org"] == params["org"] and r["owner_subject"] == params["subject"]
            ]
        assert "state_key IN {keys:Array(String)}" in sql
        assert set(params) == {"org", "ns", "keys"}
        return [
            deepcopy(r)
            for r in self.states.values()
            if (r["org"], r["namespace"]) == (params["org"], params["ns"])
            and r["state_key"] in params["keys"]
        ]


class ReadClient:
    def __init__(self, io):
        self.io = io
        self.config = SimpleNamespace(read=None, search=None)

    async def __aenter__(self):
        assert not self.io.held
        return self

    async def __aexit__(self, *args):
        return None

    async def discover(self):
        assert not self.io.held
        return SimpleNamespace(resources=self.io.readable, tools={})


def configure_authority(monkeypatch, io):
    account = ConnectedAccount(
        ORG,
        OWNER,
        SLOT,
        GEN,
        active=True,
        credential_enc="sealed",
        connected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    envelope = ConnectionEnvelope(
        version=1,
        org=ORG,
        owner=OWNER,
        slot=SLOT,
        generation=GEN,
        profile_id="reader",
        profile_sha256="c" * 64,
        endpoint="https://example.test/mcp",
        audience=None,
        auth="none",
    )
    profile = SimpleNamespace(metadata=SimpleNamespace(allow_collection_copy=True))

    async def owner(org, expected=None):
        if org != ORG or (expected is not None and io.owner != expected):
            raise ops._Refused()
        return io.owner

    def bound_client(bound, validate):
        assert bound.account == account
        assert bound.contract.generation == account.connection_id
        io.authorized.append((tuple(io.held), bound.contract))
        return ReadClient(io)

    monkeypatch.setattr(ops.connections, "_owner", owner)
    monkeypatch.setattr(ops.connections, "_ready", AsyncMock())
    monkeypatch.setattr(ops.connections, "_binding", Mock(return_value=(profile, envelope)))
    monkeypatch.setattr(ops.connections, "_bound_client", lambda current: ReadClient(io))
    monkeypatch.setattr(ops.authority.ConnectionAuthority, "client", bound_client)
    monkeypatch.setattr(ops.account_store, "get", AsyncMock(return_value=account))
    monkeypatch.setattr(ops.accounts, "_fence_current", AsyncMock(return_value=True))
    monkeypatch.setattr(ops.accounts, "_standing", AsyncMock())
    monkeypatch.setattr(
        ops.authority.aliases, "verified_spellings", AsyncMock(return_value=[OWNER])
    )
    monkeypatch.setattr(ops.authority.aliases, "invalidate", Mock())
    monkeypatch.setattr(ops.authz, "grant_role_for", AsyncMock(return_value=Role.ADMIN))
    return account


@pytest.fixture
def io(monkeypatch):
    io = SourceIO()
    monkeypatch.setattr(ops.store, "_default_client", lambda: io)
    monkeypatch.setattr(ops.scim.leases, "holding", io.lease)
    monkeypatch.setattr(ops.accounts.leases, "cluster_lease", io.lease)
    monkeypatch.setattr(ops.request_work, "mark_mirrored", lambda: True)
    monkeypatch.setattr(ops.authz, "row_in_scope", AsyncMock(return_value=True))
    monkeypatch.setattr(ops.sources, "_require_source_namespace", AsyncMock())
    monkeypatch.setattr(ops.sources, "_require_no_erasure", AsyncMock())
    monkeypatch.setattr(ops.source_push.store, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(type(ops.settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(ops.settings, "knowledge_mirrored_sources_enabled", True)
    monkeypatch.setattr(ops.settings, "knowledge_source_self_service_enabled", True)
    monkeypatch.setattr(ops.settings, "knowledge_source_sync_enabled", False)
    configure_authority(monkeypatch, io)
    return io


def add_input(**changes):
    values = dict(
        org=ORG,
        namespace=NS,
        slot=SLOT,
        request_id=NONCE,
        mode="snapshot",
        resource_ids=["opaque:A"],
        label="Private label",
    )
    values.update(changes)
    return MCPSourceAddInput(**values)


def source_input(entry):
    return MCPSourceInput(org=ORG, namespace=NS, name=entry.name)


@pytest.mark.asyncio
async def test_add_uses_actual_opaque_store_and_exact_retry(io):
    result = await ops.handle_mcp_source_add(add_input())
    entry = result.source
    row = await ops.store.latest_source(ORG, NS, entry.name)
    expected = await ops._proposed(add_input(), OWNER)
    assert row == replace(expected, created_at=row.created_at, updated_at=row.updated_at)
    assert row.config == {} and row.items == []
    assert ops._contract(row).resources[0].id == "opaque:A"
    assert row.connection_slot == SLOT and row.connection_id == GEN
    assert entry.label == "Private label" and "Private" not in entry.name
    assert result.source.refresh_state == "disabled"
    assert io.authorized and len(io.authorized[-1][0]) == 3
    assert ops.sources._require_source_namespace.await_count >= 2
    assert ops.sources._require_no_erasure.await_count >= 2
    retried = await ops.handle_mcp_source_add(add_input())
    assert retried.source == result.source and len(io.writes) == 2
    assert not retried.refresh_requested
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input(resource_ids=["opaque:B"]))
    assert len(io.writes) == 2 and not io.held


@pytest.mark.asyncio
async def test_pause_resume_preserves_identity_content_and_refuses_retry(io):
    entry = (await ops.handle_mcp_source_add(add_input())).source
    original = await ops.store.latest_source(ORG, NS, entry.name)
    paused = await ops.handle_mcp_source_pause(source_input(entry))
    row = await ops.store.latest_source(ORG, NS, entry.name)
    assert paused.source.state == "paused" and row.active and not row.pending_delete
    assert (
        row.source_id == original.source_id
        and row.generic_items_json == original.generic_items_json
    )
    assert paused.copies == "retained" and len(io.states) == 1
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_refresh(source_input(entry))
    resumed = await ops.handle_mcp_source_resume(source_input(entry))
    assert (
        resumed.source.state == "registered"
        and not ops._contract(await ops.store.latest_source(ORG, NS, entry.name)).paused
    )


@pytest.mark.asyncio
async def test_selection_and_explicit_refresh_preserve_source_id_and_state(io):
    entry = (await ops.handle_mcp_source_add(add_input())).source
    update = MCPSourceUpdateInput(
        org=ORG,
        namespace=NS,
        name=entry.name,
        mode="snapshot",
        resource_ids=["opaque:A", "opaque:B"],
        label="Renamed",
    )
    result = await ops.handle_mcp_source_update(update)
    assert result.source.source_id == entry.source_id and result.source.selected_count == 2
    assert result.refresh_requested and result.copies == "retained"
    assert len(io.states) == 1
    state = next(iter(io.states.values()))
    assert json.loads(state["detail"])["full"] is True
    before = len(io.writes)
    unchanged = await ops.handle_mcp_source_update(update)
    assert not unchanged.refresh_requested and len(io.writes) == before
    refreshed = await ops.handle_mcp_source_refresh(source_input(entry))
    assert refreshed.refresh_requested and refreshed.source.refresh_state == "disabled"
    assert len(io.writes) == before + 1


@pytest.mark.asyncio
async def test_retirement_retains_raw_rows_and_never_reuses_nonce(io, monkeypatch):
    entry = (await ops.handle_mcp_source_add(add_input())).source
    queue = AsyncMock(return_value=True)
    monkeypatch.setattr(ops.sources, "_queue_retirement", queue)
    removal = MCPSourceRemoveInput(**source_input(entry).model_dump(), confirm=entry.name)
    result = await ops.handle_mcp_source_remove(removal)
    assert result.source.state == "retired" and result.copies == "retained"
    queue.assert_not_awaited()
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    erased = await ops.handle_mcp_source_remove(
        removal.model_copy(update={"delete_documents": True})
    )
    assert erased.copies == "erasure_requested" and erased.source.pending_erasure
    queue.assert_awaited_once()
    assert (await ops.store.latest_source(ORG, NS, entry.name)).generic_items_json


@pytest.mark.asyncio
async def test_foreign_owner_has_no_metadata_or_existence(io):
    entry = (await ops.handle_mcp_source_add(add_input())).source
    io.owner = "another-person"
    listing = await ops.handle_mcp_source_list(MCPSourceListInput(org=ORG, namespace=NS))
    found = await ops.handle_mcp_source_inspect(source_input(entry))
    missing = await ops.handle_mcp_source_inspect(
        MCPSourceInput(org=ORG, namespace=NS, name="absent")
    )
    assert listing.sources == [] and found == missing
    with pytest.raises(ops._Refused) as foreign:
        await ops.handle_mcp_source_pause(source_input(entry))
    with pytest.raises(ops._Refused) as absent:
        await ops.handle_mcp_source_pause(MCPSourceInput(org=ORG, namespace=NS, name="absent"))
    assert str(foreign.value) == str(absent.value)


@pytest.mark.asyncio
async def test_self_service_disabled_blocks_add_before_discovery_but_not_existing_controls(
    io, monkeypatch
):
    entry = (await ops.handle_mcp_source_add(add_input())).source
    monkeypatch.setattr(ops.settings, "knowledge_source_self_service_enabled", False)
    discovery = AsyncMock(side_effect=AssertionError("no outbound call"))
    monkeypatch.setattr(ops.connections, "handle_mcp_discover", discovery)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    assert (await ops.handle_mcp_source_inspect(source_input(entry))).found
    assert (
        len((await ops.handle_mcp_source_list(MCPSourceListInput(org=ORG, namespace=NS))).sources)
        == 1
    )
    result = await ops.handle_mcp_source_remove(
        MCPSourceRemoveInput(
            **source_input(entry).model_dump(),
            confirm=entry.name,
        )
    )
    assert result.source.state == "retired"
    discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_only_capability_or_denied_lock_never_admits(io):
    io.readable = False
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    assert not io.writes
    io.readable = True
    io.deny_lease = True
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    assert not io.writes and not io.held


@pytest.mark.asyncio
async def test_admission_rechecks_current_destination_authority_under_locks(io, monkeypatch):
    grant = AsyncMock(return_value=Role.VIEWER)
    monkeypatch.setattr(ops.authz, "grant_role_for", grant)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    grant.assert_awaited_once_with(ORG, NS, OWNER)
    assert not io.writes and len(io.authorized[-1][0]) == 3


@pytest.mark.asyncio
async def test_collection_copy_requires_confirmation_and_records_exact_consent(io):
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input(reader_policy="collection_copy"))
    assert not io.writes
    result = await ops.handle_mcp_source_add(
        add_input(
            reader_policy="collection_copy",
            confirm_copy_authority=True,
        )
    )
    assert result.source.reader_policy == "collection_copy"
    assert io.writes


@pytest.mark.asyncio
async def test_connection_replacement_prevents_resume_or_update(io, monkeypatch):
    entry = (await ops.handle_mcp_source_add(add_input())).source
    await ops.handle_mcp_source_pause(source_input(entry))
    account = await ops.account_store.get(ORG, OWNER, SLOT)
    monkeypatch.setattr(
        ops.account_store,
        "get",
        AsyncMock(
            return_value=replace(
                account,
                connection_id="a" * 32 + "/1" + "d" * 31,
            )
        ),
    )
    before = len(io.writes)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_resume(source_input(entry))
    assert len(io.writes) == before


@pytest.mark.asyncio
async def test_unknown_contract_is_inspectable_and_retirable_without_decoding(io):
    entry = (await ops.handle_mcp_source_add(add_input())).source
    raw = io.rows[(ORG, NS, entry.name)]
    raw["source_contract_version"] = 2
    raw["config_json"] = '{"version":2,"private":"opaque"}'
    result = await ops.handle_mcp_source_inspect(source_input(entry))
    assert result.source.state == "unsupported" and result.source.resource_ids == []
    await ops.handle_mcp_source_remove(
        MCPSourceRemoveInput(
            **source_input(entry).model_dump(),
            confirm=entry.name,
        )
    )
    assert io.rows[(ORG, NS, entry.name)]["config_json"] == raw["config_json"]


@pytest.mark.asyncio
async def test_live_has_no_refresh_or_state_and_configured_limits_apply(io, monkeypatch):
    entry = (await ops.handle_mcp_source_add(add_input(mode="live"))).source
    assert entry.mode == "live" and not io.states
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_refresh(source_input(entry))
    monkeypatch.setattr(ops.settings, "knowledge_mcp_selected_resources_max", 1)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(
            add_input(
                request_id="e290f1ee-6c54-4b01-90e6-d701748f0851",
                resource_ids=["A", "B"],
            )
        )
    monkeypatch.setattr(ops.settings, "knowledge_mcp_resource_id_max_bytes", 2)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(
            add_input(
                request_id="e290f1ee-6c54-4b01-90e6-d701748f0851",
                resource_ids=["éé"],
            )
        )


@pytest.mark.asyncio
async def test_copy_consent_is_server_issued_and_passes_through_real_admission_gate(
    io, monkeypatch
):
    real = ops.authority.validate_registration
    gate = AsyncMock(wraps=real)
    monkeypatch.setattr(ops.authority, "validate_registration", gate)
    await ops.handle_mcp_source_add(
        add_input(
            reader_policy="collection_copy",
            confirm_copy_authority=True,
        )
    )
    proposed = gate.await_args.args[0]
    consent = ops._contract(proposed).copy_consent
    assert consent is not None
    assert consent.publisher == OWNER and consent.namespace == NS
    assert consent.resources_sha256 == ops.selection_sha256(ops._contract(proposed).resources)
    assert proposed.access == ops.MIRRORED_ACCESS and io.writes


@pytest.mark.parametrize(
    "field", ["owner_subject", "endpoint", "tool", "schema", "credential", "copy_consent"]
)
def test_source_api_refuses_caller_supplied_authority(field):
    from pydantic import ValidationError

    values = add_input().model_dump()
    values[field] = "private-value"
    with pytest.raises(ValidationError) as refused:
        MCPSourceAddInput.model_validate(values)
    assert "private-value" not in str(refused.value)


@pytest.mark.asyncio
async def test_label_change_does_not_refresh_and_selection_removal_retains_copies(io):
    entry = (await ops.handle_mcp_source_add(add_input(resource_ids=["A", "B"]))).source
    initial_state = deepcopy(io.states)
    label = MCPSourceUpdateInput(
        org=ORG,
        namespace=NS,
        name=entry.name,
        mode="snapshot",
        resource_ids=["A", "B"],
        label="Different label",
    )
    changed = await ops.handle_mcp_source_update(label)
    assert not changed.refresh_requested and io.states == initial_state
    smaller = await ops.handle_mcp_source_update(label.model_copy(update={"resource_ids": ["A"]}))
    assert smaller.source.source_id == entry.source_id and smaller.copies == "retained"
    assert smaller.source.resource_ids == ["A"] and not smaller.source.pending_erasure
    assert smaller.refresh_requested


@pytest.mark.asyncio
async def test_lost_add_response_reconciles_without_refresh_or_replacement(io, monkeypatch):
    real_insert = io.insert

    async def lost(table, rows):
        await real_insert(table, rows)
        if table == ops.store.TABLE:
            raise RuntimeError("lost response after successful insert")

    monkeypatch.setattr(io, "insert", lost)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    assert len(io.rows) == 1 and len(io.states) == 1
    before = deepcopy(io.writes)
    result = await ops.handle_mcp_source_add(add_input())
    assert result.source.source_id == next(iter(io.rows.values()))["source_id"]
    assert not result.refresh_requested and io.writes == before


@pytest.mark.asyncio
async def test_grant_revocation_before_lock_acquisition_prevents_writes(io, monkeypatch):
    async def grant(org, namespace, role):
        return not io.held

    monkeypatch.setattr(ops.authz, "row_in_scope", grant)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    assert not io.writes and not io.held


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["_require_source_namespace", "_require_no_erasure"])
async def test_invalid_destination_or_erasure_refuses_before_discovery(io, monkeypatch, helper):
    monkeypatch.setattr(ops.sources, helper, AsyncMock(side_effect=ops._Refused()))
    discovery = AsyncMock(side_effect=AssertionError("no outbound call"))
    monkeypatch.setattr(ops.connections, "handle_mcp_discover", discovery)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    discovery.assert_not_awaited()
    assert not io.writes


@pytest.mark.asyncio
async def test_actual_source_quota_helper_prevents_source_and_state_writes(io, monkeypatch):
    monkeypatch.setattr(ops.settings, "knowledge_connected_sources_max_per_person", 0)
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_add(add_input())
    assert not io.writes and not io.held


def test_audit_subject_does_not_disclose_selection_labels_or_counts():
    from server.knowledge.api.ops_mcp_sources import OPERATIONS

    for operation in OPERATIONS:
        if operation.audit:
            assert operation.audit_subject(add_input(), object()) == {"kind": "mcp"}


@pytest.mark.asyncio
async def test_scope_revision_blocks_aba_and_stale_selection_updates(io):
    entry = (await ops.handle_mcp_source_add(add_input(resource_ids=["A"]))).source
    update = MCPSourceUpdateInput(
        org=ORG,
        namespace=NS,
        name=entry.name,
        mode="snapshot",
        resource_ids=["B"],
        if_scope_revision=1,
    )
    second = (await ops.handle_mcp_source_update(update)).source
    assert second.scope_revision == 2
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_update(update.model_copy(update={"resource_ids": ["A"]}))
    assert ops._contract(await ops.store.latest_source(ORG, NS, entry.name)).scope_revision == 2
    third = (
        await ops.handle_mcp_source_update(
            update.model_copy(update={"resource_ids": ["A"], "if_scope_revision": 2})
        )
    ).source
    assert third.scope_revision == 3
    assert third.source_id == entry.source_id


def test_update_cannot_change_publication_audience():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MCPSourceUpdateInput(
            org=ORG,
            namespace=NS,
            name="mcp-opaque",
            mode="snapshot",
            resource_ids=["A"],
            reader_policy="collection_copy",
            confirm_copy_authority=True,
        )


@pytest.mark.asyncio
async def test_list_pages_are_owner_namespace_bound_and_omit_resource_ids(io):
    entries = []
    for i in range(3):
        entries.append(
            (
                await ops.handle_mcp_source_add(
                    add_input(
                        request_id=f"00000000-0000-4000-8000-{i:012d}",
                        resource_ids=[f"private:{i}"],
                    )
                )
            ).source
        )
    first = await ops.handle_mcp_source_list(MCPSourceListInput(org=ORG, namespace=NS, limit=2))
    assert len(first.sources) == 2 and first.next_cursor
    assert "private:" not in first.model_dump_json()
    assert "resource_ids" not in first.model_dump_json()
    second = await ops.handle_mcp_source_list(
        MCPSourceListInput(org=ORG, namespace=NS, limit=2, cursor=first.next_cursor)
    )
    assert len(second.sources) == 1 and second.next_cursor is None
    assert {s.source_id for s in first.sources + second.sources} == {s.source_id for s in entries}
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_list(
            MCPSourceListInput(org=ORG, namespace=NS, limit=1, cursor=first.next_cursor)
        )
    io.owner = "another-person"
    with pytest.raises(ops._Refused):
        await ops.handle_mcp_source_list(
            MCPSourceListInput(org=ORG, namespace=NS, limit=2, cursor=first.next_cursor)
        )
    empty = await ops.handle_mcp_source_list(MCPSourceListInput(org=ORG, namespace=NS))
    assert empty.sources == [] and empty.next_cursor is None
