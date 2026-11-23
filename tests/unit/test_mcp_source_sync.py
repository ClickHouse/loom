"""Selected MCP execution with real SDK reads and in-memory storage seams."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from server.clients import mcp_source as client_module
from server.clients.mcp_source import MCPSourceClient, MCPSourceError
from server.code.app.index.pass_budget import PassBudget
from server.knowledge.app import mcp_connections, mcp_profiles, sources, writes
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import source_sync as sync
from server.knowledge.app import source_sync_mcp as engine
from server.knowledge.domain import access_list
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.domain.mcp_sources import (
    ConnectionEnvelope,
    ResourceIdentity,
    SourceContract,
    encode_envelope,
    encode_source,
)
from server.knowledge.domain.sources import MIRRORED_ACCESS, KnowledgeSource
from server.knowledge.store import blobs, chunks, documents
from server.memory.domain.provenance import pipeline_actor
from server.operations.base import Role

MIRROR = "acl-" + "a" * 24
RESOURCE = "selected://document/one"
SECOND = "selected://document/two"
BINARY = "selected://document/binary"
TOOL_INPUT = {
    "type": "object",
    "properties": {"resource_id": {"type": "string"}},
    "required": ["resource_id"],
    "additionalProperties": False,
}
TOOL_OUTPUT = {
    "type": "object",
    "properties": {key: {"type": "string"} for key in ("id", "title", "text", "uri")},
    "required": ["id", "title", "text", "uri"],
    "additionalProperties": False,
}


@dataclass
class Harness:
    source: KnowledgeSource
    account: ConnectedAccount
    contract: SourceContract
    saved: dict[str, Any] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    doc_rows: list[Any] = field(default_factory=list)
    chunk_rows: list[Any] = field(default_factory=list)
    originals: list[bytes] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    locks: list[str] = field(default_factory=list)
    listing: AsyncMock = field(default_factory=AsyncMock)
    failing_resource: str = ""
    text: str = "Selected resource text."

    def select(
        self,
        *,
        mode: str = "snapshot",
        policy: str = "owner_only",
        two: bool = False,
        resources: tuple[str, ...] | None = None,
    ) -> None:
        payload = self.contract.model_dump(mode="json")
        payload.update(mode=mode, reader_policy=policy)
        if two:
            payload["resources"].append({"type": "resource", "id": SECOND})
        if resources is not None:
            payload["resources"] = [{"type": "resource", "id": item} for item in resources]
        self.contract = SourceContract.model_validate_json(json.dumps(payload))
        config, items = encode_source(self.contract)
        self.source = replace(self.source, generic_config_json=config, generic_items_json=items)


def _catalog(tool: bool) -> mcp_profiles.MCPProfileCatalog:
    config: dict[str, Any] = {"endpoint": "https://selected.example/mcp"}
    if tool:
        config.update(
            approved_tools=["read_selected"],
            read={
                "name": "read_selected",
                "argument_name": "resource_id",
                "input_schema": TOOL_INPUT,
                "output_schema": TOOL_OUTPUT,
            },
        )
    return mcp_profiles.MCPProfileCatalog.parse(
        json.dumps(
            {
                "version": 1,
                "profiles": [
                    {
                        "id": "approved",
                        "label": "Selected",
                        "auth": "none",
                        "allowed_orgs": ["org"],
                        "allow_collection_copy": True,
                        "config": config,
                    }
                ],
            }
        )
    )


def test_authority_reads_the_actual_opaque_store_representation(harness: Harness) -> None:
    config, items = encode_source(harness.contract)
    stored = authority.source_store._source(
        {
            "org": harness.source.org,
            "namespace": harness.source.namespace,
            "name": harness.source.name,
            "source_id": harness.source.source_id,
            "source_kind": "mcp",
            "credential_mode": "connected",
            "owner_subject": harness.source.owner_subject,
            "connection_id": harness.contract.generation,
            "connection_slot": harness.contract.slot,
            "source_contract_version": 1,
            "config_json": config,
            "items_json": items,
            "active": True,
            "access": MIRRORED_ACCESS,
        }
    )
    assert stored.config == {} and stored.items == []
    assert authority.source_contract(stored) == harness.contract


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Harness:
    catalog = _catalog(getattr(request, "param", "") == "tool")
    profile = catalog.get("org", "approved")
    slot, generation = "mcp:" + "a" * 32, "a" * 32 + "/1" + "b" * 31
    contract = SourceContract(
        version=1,
        slot=slot,
        generation=generation,
        profile_id="approved",
        profile_sha256=profile.metadata.profile_sha256,
        mode="snapshot",
        reader_policy="owner_only",
        resources=(ResourceIdentity(type="resource", id=RESOURCE),),
    )
    config, items = encode_source(contract)
    source = KnowledgeSource(
        "org",
        "collection",
        "selected",
        "mcp",
        "00000000-0000-4000-8000-000000000001",
        generic_config_json=config,
        generic_items_json=items,
        access=MIRRORED_ACCESS,
        credential_mode="connected",
        owner_subject="owner",
        connection_id=generation,
        source_contract_version=1,
        connection_slot=slot,
    )
    account = ConnectedAccount(
        "org",
        "owner",
        slot,
        generation,
        active=True,
        connected_at=datetime.now(UTC),
        credential_enc="sealed-fixture",
    )
    h = Harness(source, account, contract)
    envelope = ConnectionEnvelope(
        version=1,
        org="org",
        owner="owner",
        slot=slot,
        generation=generation,
        profile_id="approved",
        profile_sha256=profile.metadata.profile_sha256,
        endpoint="https://selected.example/mcp",
        audience=None,
        auth="none",
    )
    monkeypatch.setattr(mcp_connections, "unseal", lambda *a, **k: encode_envelope(envelope))
    monkeypatch.setattr(mcp_connections, "load_mcp_profile_catalog", lambda: catalog)
    monkeypatch.setattr(mcp_connections, "_ready", AsyncMock())
    monkeypatch.setattr(authority.account_store, "get", AsyncMock(side_effect=lambda *a: h.account))
    monkeypatch.setattr(
        authority.source_store, "get_source", AsyncMock(side_effect=lambda *a: h.source)
    )
    _owner_seams(monkeypatch)
    _lock_seams(monkeypatch, h)
    _mirror_seams(monkeypatch, h)
    return h


def _owner_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    enabled = SimpleNamespace(
        auth_actually_required=lambda: True, knowledge_mirrored_sources_enabled=True
    )
    monkeypatch.setattr(authority, "settings", enabled)
    monkeypatch.setattr(authority.accounts, "settings", enabled)
    monkeypatch.setattr(
        authority.accounts,
        "resolve_reader",
        AsyncMock(return_value=SimpleNamespace(subject="owner", emails=frozenset())),
    )
    monkeypatch.setattr(authority.aliases, "verified_spellings", AsyncMock(return_value=["owner"]))
    monkeypatch.setattr(authority.aliases, "invalidate", lambda *a: None)
    monkeypatch.setattr(authority.account_store, "account_active", AsyncMock(return_value=True))
    monkeypatch.setattr(authority.accounts.scim, "refuses_subject", AsyncMock(return_value=False))
    monkeypatch.setattr(authority.accounts.revocation, "cutoffs", AsyncMock(return_value={}))
    monkeypatch.setattr(authority.accounts, "_fence_current", AsyncMock(return_value=True))
    monkeypatch.setattr(authority.authz, "grant_role_for", AsyncMock(return_value=Role.WRITER))
    monkeypatch.setattr(sources, "_require_source_namespace", AsyncMock())
    monkeypatch.setattr(sources, "_require_no_erasure", AsyncMock())


def _lock_seams(monkeypatch: pytest.MonkeyPatch, h: Harness) -> None:
    account_lock, namespace_lock = asyncio.Lock(), asyncio.Lock()

    @asynccontextmanager
    async def account(*args: Any) -> AsyncIterator[None]:
        assert args == (h.source.org, h.source.owner_subject, h.contract.slot)
        async with account_lock:
            h.locks.append("account")
            h.events.append("account")
            try:
                yield
            finally:
                h.locks.pop()

    @asynccontextmanager
    async def namespace(*args: Any) -> AsyncIterator[None]:
        assert h.locks == ["account"]
        async with namespace_lock:
            h.locks.append("namespace")
            h.events.append("namespace")
            try:
                yield
            finally:
                h.locks.pop()

    monkeypatch.setattr(authority.accounts, "account_lease", account)
    monkeypatch.setattr(sources, "sources_lease", namespace)


def _mirror_seams(monkeypatch: pytest.MonkeyPatch, h: Harness) -> None:
    monkeypatch.setattr(engine.mirrors, "live_mirror", AsyncMock(return_value=MIRROR))
    monkeypatch.setattr(
        sync, "mirror_state", AsyncMock(side_effect=lambda *a: {"detail": json.dumps(h.saved)})
    )
    monkeypatch.setattr(sync.store, "source_revisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(documents, "publications_owed", AsyncMock(return_value=False))
    monkeypatch.setattr(engine.mirror_store, "record_listing", h.listing)
    monkeypatch.setattr(engine.private_sync, "write_private_cost_event_for", AsyncMock())


@pytest_asyncio.fixture
async def upstream(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastMCP]:
    server = FastMCP(
        "selected",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["selected.example", "198.51.100.1"],
            allowed_origins=["https://selected.example", "https://198.51.100.1"],
        ),
    )

    @server.resource(RESOURCE)
    async def first() -> str:
        harness.reads.append(RESOURCE)
        return harness.text

    @server.resource(SECOND)
    async def second() -> str:
        harness.reads.append(SECOND)
        if harness.failing_resource == SECOND:
            raise ValueError("synthetic unavailable resource")
        return "Second selected text."

    @server.resource(BINARY, mime_type="application/octet-stream")
    async def binary() -> bytes:
        harness.reads.append(BINARY)
        return b"\x00\x01"

    _tools(server, harness)
    app = server.streamable_http_app()
    original_builder = authority.build_mcp_source_client

    def build(*args: Any, **kwargs: Any) -> MCPSourceClient:
        prototype = original_builder(*args, **kwargs)
        return MCPSourceClient(
            prototype.config,
            prototype.credential,
            validate_access=prototype.validate_access,
            _transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr(authority, "build_mcp_source_client", build)
    monkeypatch.setattr(
        client_module,
        "pinned_target",
        AsyncMock(
            return_value=("", "https://198.51.100.1/mcp", "selected.example", "selected.example")
        ),
    )
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def lifetime() -> None:
        async with server.session_manager.run():
            ready.set_result(None)
            await stop.wait()

    owner = asyncio.create_task(lifetime())
    await ready
    try:
        yield server
    finally:
        stop.set()
        await owner


def _tools(server: FastMCP, h: Harness) -> None:
    @server._mcp_server.list_tools()
    async def listed() -> list[types.Tool]:
        return [types.Tool(name="read_selected", inputSchema=TOOL_INPUT, outputSchema=TOOL_OUTPUT)]

    @server._mcp_server.call_tool()
    async def called(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        assert name == "read_selected" and arguments == {"resource_id": RESOURCE}
        h.reads.append("tool:" + arguments["resource_id"])
        value = {
            "id": RESOURCE,
            "title": "Selected",
            "text": "Mapped selected text.",
            "uri": "https://untrusted.example/other",
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(value))],
            structuredContent=value,
        )


@pytest.fixture
def storage(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = harness
    monkeypatch.setattr(
        writes, "get_embedder", lambda: SimpleNamespace(dim=2, model_name="fixture")
    )
    monkeypatch.setattr(writes.codes_state, "refuse_width", AsyncMock())
    monkeypatch.setattr(
        writes,
        "_embed_inline_batch",
        AsyncMock(side_effect=lambda embedder, texts: [[0.0, 1.0] for _ in texts]),
    )
    monkeypatch.setattr(chunks, "find_doc_by_content_sha256", AsyncMock(return_value=None))
    monkeypatch.setattr(chunks, "count_active_for_doc", AsyncMock(return_value=0))
    monkeypatch.setattr(chunks, "count_matching_chunk_hashes", AsyncMock(return_value=0))
    monkeypatch.setattr(documents, "get_one", AsyncMock(return_value=None))
    monkeypatch.setattr(writes, "_drop_own_chunks", AsyncMock())

    async def blob(org: str, namespace: str, raw: bytes, **kwargs: Any) -> str:
        assert h.locks == ["account", "namespace"]
        h.originals.append(raw)
        return "original/selected"

    async def insert_chunks(rows: list[Any]) -> None:
        assert h.locks == ["account", "namespace"]
        h.chunk_rows.extend(rows)

    async def insert_document(row: Any, **kwargs: Any) -> None:
        assert h.locks == ["account", "namespace"]
        h.doc_rows.append(row)

    monkeypatch.setattr(blobs, "put", blob)
    monkeypatch.setattr(chunks, "insert_chunks", insert_chunks)
    monkeypatch.setattr(documents, "insert_one", insert_document)
    return h


async def execute(
    h: Harness, *, limit: int = 10, state: dict[str, str] | None = None
) -> sync.TurnResult:
    started = time.monotonic()
    with pipeline_actor(sync.SYNC_WORKER_ACTOR):
        return await engine.turn(
            h.source, state or {}, sync.Pass(PassBudget(started, started + 15), limit)
        )


@pytest.mark.asyncio
async def test_selected_sdk_read_uses_real_upsert_rows_and_owner_access(
    storage: Harness,
    upstream: FastMCP,
) -> None:
    h = storage
    result = await execute(h)
    assert (result.status, result.complete, result.full, result.removed) == ("ok", True, False, 0)
    assert h.reads == [RESOURCE]
    assert h.originals == [b"Selected resource text."]
    assert len(h.doc_rows) == len(h.chunk_rows) == 1
    document, chunk = h.doc_rows[0], h.chunk_rows[0]
    uri = resource_uri(h.source.source_id, RESOURCE)
    assert (document["org"], document["namespace"], document["source_uri"]) == ("org", MIRROR, uri)
    assert document["content"] == chunk["content"] == "Selected resource text."
    assert document["source_class"] == chunk["source_class"] == "imported"
    assert document["actor"] == sync.SYNC_WORKER_ACTOR
    assert (document["user_id"], document["session_id"], document["agent"]) == ("-", "-", "-")
    assert access_list.from_json(document["access_json"]) == access_list.AccessList(owner="owner")
    assert document["blob_path"] == "original/selected"
    assert document["blob_size"] == len(h.originals[0])
    assert document["revision"] == 1
    assert document["version"] == chunk["doc_version"]
    metadata = json.loads(document["metadata"])
    assert metadata["source_revision"] == ""
    assert metadata["ingested_from"] == RESOURCE
    assert metadata["mcp_read_operation"] == "resources/read"
    assert metadata["mcp_connection_generation"] == h.contract.generation
    assert document["source_uri"] == chunk["source_uri"] == uri
    assert "source-id:" + h.source.source_id in document["tags"]
    h.listing.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_completion_and_bounded_resume(storage: Harness, upstream: FastMCP) -> None:
    h = storage
    h.select(two=True)
    first = await execute(h, limit=1)
    assert first.cut and not first.complete and first.kind_state["mcp_next_index"] == 1
    h.listing.assert_not_awaited()
    h.saved = first.kind_state
    second = await execute(h)
    assert second.complete and h.reads == [RESOURCE, SECOND]
    h.saved = second.kind_state
    await execute(h)
    assert h.reads == [RESOURCE, SECOND]
    h.listing.assert_awaited_once()


@pytest.mark.asyncio
async def test_selected_refresh_and_explicit_snapshot_request(
    storage: Harness, upstream: FastMCP
) -> None:
    h = storage
    first = await execute(h, state={"detail": '{"requested_at":"request-one"}'})
    h.saved = first.kind_state
    await execute(h)
    assert h.reads == [RESOURCE]
    await execute(h, state={"detail": '{"requested_at":"request-two"}'})
    assert h.reads == [RESOURCE, RESOURCE]
    h.select(mode="selected_refresh")
    first = await execute(h)
    h.saved = first.kind_state
    second = await execute(h)
    assert second.complete and not second.full and h.reads == [RESOURCE] * 4


@pytest.mark.asyncio
async def test_live_and_exhausted_turns_publish_nothing(storage: Harness) -> None:
    h = storage
    h.select(mode="live")
    result = await execute(h)
    assert result.complete and result.mirror == "" and not result.full
    h.select(mode="snapshot")
    result = await execute(h, limit=0)
    assert result.cut and not result.complete
    assert not h.doc_rows and not h.originals
    h.listing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"org": "other"},
        {"namespace": "other"},
        {"source_id": "00000000-0000-4000-8000-000000000002"},
        {"owner_subject": "other"},
        {"connection_id": "a" * 32 + "/1" + "c" * 31},
        {"active": False},
        {"source_contract_version": 2},
        {"generic_items_json": "[]"},
        {"generic_config_json": "{}"},
        {"items": [{"type": "resource", "id": SECOND}]},
        {"config": {"mode": "live"}},
        {"connection_slot": "mcp:" + "c" * 32},
        {"access": "namespace"},
        {"credential_mode": "shared"},
    ],
)
async def test_exact_stored_source_is_required(harness: Harness, change: dict[str, Any]) -> None:
    expected = harness.source
    harness.source = replace(expected, **change)
    with pytest.raises(authority.MCPAuthorityError, match="^MCP_SOURCE_UNAVAILABLE$"):
        await authority.validate_source(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"active": False},
        {"credential_enc": ""},
        {"refresh_id": "pending"},
        {"connection_id": "a" * 32 + "/1" + "c" * 31},
        {"connected_at": None},
        {"subject": "other"},
        {"org": "other"},
        {"kind": "mcp:" + "c" * 32},
    ],
)
async def test_account_binding_must_still_be_active(
    harness: Harness, change: dict[str, Any]
) -> None:
    harness.account = replace(harness.account, **change)
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_source(harness.source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seam", ["fence", "owner", "cutoff", "grant", "profile", "destination", "erasure"]
)
async def test_current_authority_refusals(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    if seam == "fence":
        monkeypatch.setattr(authority.accounts, "_fence_current", AsyncMock(return_value=False))
    elif seam == "owner":
        monkeypatch.setattr(
            authority.account_store, "account_active", AsyncMock(return_value=False)
        )
    elif seam == "cutoff":
        assert harness.account.connected_at is not None
        monkeypatch.setattr(
            authority.accounts.revocation,
            "cutoffs",
            AsyncMock(
                return_value={
                    "owner": int(harness.account.connected_at.timestamp()) + 1,
                }
            ),
        )
    elif seam == "grant":
        monkeypatch.setattr(authority.authz, "grant_role_for", AsyncMock(return_value=None))
    elif seam == "profile":
        monkeypatch.setattr(
            mcp_connections, "load_mcp_profile_catalog", lambda: mcp_profiles.MCPProfileCatalog()
        )
    else:
        target = "_require_source_namespace" if seam == "destination" else "_require_no_erasure"
        monkeypatch.setattr(sources, target, AsyncMock(side_effect=ValueError("private detail")))
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_source(harness.source)


@pytest.mark.asyncio
async def test_collection_copy_is_refused_even_with_operator_allow(harness: Harness) -> None:
    harness.select(policy="collection_copy")
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_source(harness.source)


@pytest.mark.asyncio
async def test_registration_checks_do_not_grant_unstored_source_execution(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority.source_store, "get_source", AsyncMock(return_value=None))
    bound = await authority.validate_registration(harness.source)
    assert bound.contract == harness.contract
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_source(harness.source)
    harness.select(policy="collection_copy")
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_registration(harness.source)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", ["source", "account", "grant", "profile", "owner"])
async def test_source_replacement_between_original_and_commit_refuses(
    storage: Harness,
    upstream: FastMCP,
    monkeypatch: pytest.MonkeyPatch,
    lost: str,
) -> None:
    h = storage
    original = writes._embed_inline_batch

    async def replace_source(*args: Any, **kwargs: Any) -> Any:
        assert not h.locks
        if lost == "source":
            h.source = replace(h.source, active=False)
        elif lost == "account":
            h.account = replace(h.account, active=False)
        elif lost == "grant":
            monkeypatch.setattr(authority.authz, "grant_role_for", AsyncMock(return_value=None))
        elif lost == "profile":
            monkeypatch.setattr(
                mcp_connections,
                "load_mcp_profile_catalog",
                lambda: mcp_profiles.MCPProfileCatalog(),
            )
        else:
            monkeypatch.setattr(
                authority.account_store, "account_active", AsyncMock(return_value=False)
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(writes, "_embed_inline_batch", replace_source)
    result = await execute(h)
    assert not result.complete and result.status == "unavailable"
    assert h.originals == [b"Selected resource text."]
    assert not h.doc_rows and not h.chunk_rows
    h.listing.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_guard_keeps_locks_and_preserves_body_exceptions(harness: Harness) -> None:
    src = replace(harness.source, mirror=MIRROR)
    with pytest.raises(RuntimeError, match="commit result"):
        async with authority.publication_guard(src):
            assert harness.locks == ["account", "namespace"]
            raise RuntimeError("commit result")
    assert not harness.locks


@pytest.mark.asyncio
async def test_actual_receipt_rejects_tampering_and_unselected_reads(
    harness: Harness,
    upstream: FastMCP,
) -> None:
    bound = await authority.validate_source(harness.source)
    client = bound.client()
    async with client:
        evidence = await client.read(RESOURCE)
        assert engine._receipt(client, RESOURCE, evidence) == evidence.receipt.content_sha256
        with pytest.raises(MCPSourceError):
            engine._receipt(client, RESOURCE, replace(evidence, text="injected"))
        with pytest.raises(MCPSourceError):
            engine._receipt(client, SECOND, evidence)
        with pytest.raises(MCPSourceError):
            engine._receipt(client, RESOURCE, replace(evidence, receipt=None))
        assert evidence.receipt is not None
        for receipt in (
            replace(evidence.receipt, identity_verified=False),
            replace(evidence.receipt, operation="tools/call"),
            replace(evidence.receipt, config_sha256="0" * 64),
        ):
            with pytest.raises(MCPSourceError):
                engine._receipt(client, RESOURCE, replace(evidence, receipt=receipt))
        with pytest.raises(MCPSourceError):
            await client.read(SECOND)
    assert harness.reads == [RESOURCE]


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["tool"], indirect=True)
async def test_explicit_tool_mapping_cannot_change_citation(
    storage: Harness, upstream: FastMCP
) -> None:
    result = await execute(storage)
    assert result.complete and result.status == "ok"
    assert storage.reads == ["tool:" + RESOURCE]
    assert storage.doc_rows[0]["source_uri"] == resource_uri(storage.source.source_id, RESOURCE)
    assert storage.originals == [b"Mapped selected text."]
    assert json.loads(storage.doc_rows[0]["metadata"])["mcp_read_operation"] == "tools/call"


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [BINARY, "selected://document/absent"])
async def test_unreadable_selected_resources_do_not_publish_or_delete(
    storage: Harness,
    upstream: FastMCP,
    monkeypatch: pytest.MonkeyPatch,
    selected: str,
) -> None:
    retire = AsyncMock(side_effect=AssertionError("no deletion authority"))
    monkeypatch.setattr(sync, "retire", retire)
    storage.select(resources=(selected,))
    result = await execute(storage)
    assert not result.complete and result.removed == 0 and result.status == "failed"
    assert result.kind_state["mcp_next_index"] == 0
    assert not storage.originals and not storage.chunk_rows and not storage.doc_rows
    storage.listing.assert_not_awaited()
    retire.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_set_keeps_success_and_retries_only_failure(
    storage: Harness, upstream: FastMCP
) -> None:
    h = storage
    h.select(two=True)
    h.failing_resource = SECOND
    result = await execute(h)
    assert not result.complete and result.kind_state["mcp_next_index"] == 1 and result.removed == 0
    assert len(h.doc_rows) == 1
    h.listing.assert_not_awaited()
    h.saved, h.failing_resource = result.kind_state, ""
    result = await execute(h)
    assert result.complete and len(h.doc_rows) == 2
    assert h.reads == [RESOURCE, SECOND, SECOND]


@pytest.mark.asyncio
async def test_changed_selection_does_not_resume_another_set(
    storage: Harness, upstream: FastMCP
) -> None:
    h = storage
    h.select(two=True)
    h.saved = (await execute(h, limit=1)).kind_state
    h.select(resources=(SECOND, RESOURCE))
    result = await execute(h, limit=1)
    assert not result.complete and result.kind_state["mcp_next_index"] == 1
    assert h.reads == [RESOURCE, SECOND]
    h.listing.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_contract_refuses_direct_knowledge_publication(storage: Harness) -> None:
    storage.select(mode="live")
    with pytest.raises(authority.MCPAuthorityError):
        await sync.write_document(
            replace(storage.source, mirror=MIRROR),
            {"id": RESOURCE, "name": "selected"},
            resource_uri(storage.source.source_id, RESOURCE),
            "text",
            ingested_format="mcp_text",
            original=b"text",
        )
    assert not storage.originals and not storage.chunk_rows and not storage.doc_rows


@pytest.mark.asyncio
async def test_empty_selected_text_does_not_delete_or_publish(
    storage: Harness, upstream: FastMCP
) -> None:
    storage.text = ""
    result = await execute(storage)
    assert result.status == "failed" and not result.complete and result.removed == 0
    assert result.kind_state["mcp_next_index"] == 0
    assert not storage.originals and not storage.doc_rows and not storage.chunk_rows
    storage.listing.assert_not_awaited()
