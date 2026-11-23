"""Official SDK live results stay selected, private and publication checked."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types

from server.auth.context import current_auth_var
from server.clients.mcp_source import MCPSourceError
from server.knowledge.app import live_search, mcp_connections, mcp_live, mcp_profiles, writes
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app.mcp_contracts import encode_source
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.domain.mcp_sources import ConnectionEnvelope, encode_envelope
from tests.unit.test_mcp_source_read_policy import reader as reader
from tests.unit.test_mcp_source_sync import RESOURCE, SECOND, TOOL_OUTPUT
from tests.unit.test_mcp_source_sync import harness as harness
from tests.unit.test_mcp_source_sync import upstream as upstream

SEARCH_INPUT = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}
SEARCH_OUTPUT = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": TOOL_OUTPUT}},
    "required": ["items"],
    "additionalProperties": False,
}


@pytest.fixture
def live_setup(reader, harness, monkeypatch):
    catalog = mcp_profiles.MCPProfileCatalog.parse(
        json.dumps(
            {
                "version": 1,
                "profiles": [
                    {
                        "id": "approved",
                        "label": "Reader",
                        "auth": "none",
                        "allowed_orgs": ["org"],
                        "config": {
                            "endpoint": "https://selected.example/mcp",
                            "approved_tools": ["lookup"],
                            "search": {
                                "name": "lookup",
                                "argument_name": "query",
                                "input_schema": SEARCH_INPUT,
                                "output_schema": SEARCH_OUTPUT,
                                "items_path": ["items"],
                            },
                        },
                    }
                ],
            }
        )
    )
    profile = catalog.get("org", "approved")
    harness.contract = harness.contract.model_copy(
        update={
            "mode": "live",
            "profile_sha256": profile.metadata.profile_sha256,
        }
    )
    config, items = encode_source(harness.contract)
    harness.source = replace(harness.source, generic_config_json=config, generic_items_json=items)
    envelope = ConnectionEnvelope(
        version=1,
        org="org",
        owner="owner",
        slot=harness.contract.slot,
        generation=harness.contract.generation,
        profile_id="approved",
        profile_sha256=profile.metadata.profile_sha256,
        endpoint="https://selected.example/mcp",
        audience=None,
        auth="none",
    )
    monkeypatch.setattr(mcp_connections, "load_mcp_profile_catalog", lambda: catalog)
    monkeypatch.setattr(mcp_connections, "unseal", lambda *a, **k: encode_envelope(envelope))
    monkeypatch.setattr(
        writes,
        "handle_knowledge_upsert",
        AsyncMock(side_effect=AssertionError("live retrieval must not import")),
    )
    return SimpleNamespace(h=harness, auth=reader.auth, schema=SEARCH_OUTPUT)


@pytest.fixture
def live_tools(live_setup, upstream):
    state = live_setup

    @upstream._mcp_server.list_tools()
    async def listed():
        return [types.Tool(name="lookup", inputSchema=SEARCH_INPUT, outputSchema=state.schema)]

    @upstream._mcp_server.call_tool()
    async def called(name, arguments):
        assert name == "lookup" and arguments == {"query": "selected"}
        rows = [
            {
                "id": value,
                "title": "Untrusted search title",
                "text": "Search is not a read",
                "uri": "https://untrusted.example/",
            }
            for value in (SECOND, RESOURCE, RESOURCE)
        ]
        return types.CallToolResult(content=[], structuredContent={"items": rows})

    return state


@pytest.mark.asyncio
async def test_live_search_reads_only_selected_ids_and_imports_nothing(live_tools):
    h, auth = live_tools.h, live_tools.auth
    result = await mcp_live.search(h.source, auth, "selected", limit=2)
    assert result is not None and len(result.hits) == 1
    hit = result.hits[0]
    assert hit.id == hit.url == resource_uri(h.source.source_id, RESOURCE)
    assert hit.provider == "mcp" and hit.resource_identity_verified
    assert hit.content == h.text and hit.content != "Search is not a read"
    assert h.reads == [RESOURCE] and not h.doc_rows and not h.originals
    writes.handle_knowledge_upsert.assert_not_awaited()
    await result.check()
    assert h.reads == [RESOURCE, RESOURCE]


@pytest.mark.asyncio
async def test_live_result_loses_publication_when_the_resource_changes(live_tools):
    result = await mcp_live.search(live_tools.h.source, live_tools.auth, "selected", limit=1)
    live_tools.h.text = "Changed after retrieval"
    with pytest.raises(MCPSourceError):
        await result.check()
    assert live_tools.h.account.active


@pytest.mark.asyncio
async def test_live_revalidation_detects_schema_drift(live_tools):
    result = await mcp_live.search(live_tools.h.source, live_tools.auth, "selected", limit=1)
    live_tools.schema = {**SEARCH_OUTPUT, "description": "Changed contract"}
    with pytest.raises(MCPSourceError):
        await result.check()
    assert not live_tools.h.account.active


@pytest.mark.asyncio
async def test_another_user_cannot_open_the_owned_live_source(live_tools):
    other = replace(live_tools.auth, subject="other")
    current_auth_var.set(other)
    with pytest.raises(authority.MCPAuthorityError):
        await mcp_live.search(live_tools.h.source, other, "selected", limit=1)
    assert live_tools.h.reads == []


@pytest.mark.asyncio
async def test_local_revocation_blocks_retained_live_results_before_another_read(
    live_tools, monkeypatch
):
    result = await mcp_live.search(live_tools.h.source, live_tools.auth, "selected", limit=1)
    monkeypatch.setattr(authority.accounts, "_fence_current", AsyncMock(return_value=False))
    with pytest.raises(authority.MCPAuthorityError):
        await result.check()
    assert live_tools.h.reads == [RESOURCE]


@pytest.mark.asyncio
async def test_existing_live_pipeline_retains_its_final_publication_check(live_tools, monkeypatch):
    h = live_tools.h
    monkeypatch.setattr(
        live_search,
        "settings",
        SimpleNamespace(
            auth_actually_required=lambda: True,
            knowledge_mirrored_sources_enabled=True,
            knowledge_live_search_timeout_s=10,
            knowledge_live_search_max_sources=4,
            knowledge_live_search_max_hits=5,
        ),
    )
    monkeypatch.setattr(live_search, "protected_for", lambda auth: True)
    monkeypatch.setattr(live_search.request_tail, "required_active", lambda: True)
    monkeypatch.setattr(live_search.request_work, "mark_mirrored", lambda: True)
    monkeypatch.setattr(live_search.session_admission, "require", AsyncMock())
    monkeypatch.setattr(
        live_search.connected_accounts, "own_subject", AsyncMock(return_value=("org", "owner"))
    )
    monkeypatch.setattr(
        live_search.source_store, "connected_sources", AsyncMock(return_value=[h.source])
    )
    retained = []
    monkeypatch.setattr(
        live_search.request_tail, "retain_publication", lambda *args: retained.append(args) or True
    )
    result = await live_search.search("org", ["collection"], "selected", top_k=1)
    assert result is not None and not result.fresh()
    assert await live_search.publish(result) is result and result.fresh()
    assert retained == [(result, result.check, result.fresh)]
    h.text = "The remote result changed before final release"
    with pytest.raises(MCPSourceError):
        await retained[0][1]()
    assert not result.fresh()


@pytest.mark.asyncio
async def test_cancelled_live_rpc_is_joined(live_tools, upstream):
    started, stopped = asyncio.Event(), asyncio.Event()

    @upstream._mcp_server.call_tool()
    async def blocked(name, arguments):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(
        mcp_live.search(live_tools.h.source, live_tools.auth, "selected", limit=1)
    )
    async with asyncio.timeout(5):
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stopped.wait()
    assert not live_tools.h.reads and not live_tools.h.doc_rows


def test_expired_retained_live_result_is_not_fresh():
    result = live_search.LiveResult((), time.monotonic() - 1, _checked=True)
    assert not result.fresh()
