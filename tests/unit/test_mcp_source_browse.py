"""Owner-bound metadata uses real SDK sessions without importing or reading bodies."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from mcp import types
from pydantic import ValidationError as ModelError

from server.auth.context import current_auth_var
from server.core import request_tail
from server.core.diagnostics import codes_only, public_diagnostics
from server.core.errors import ValidationError
from server.knowledge.api.models_mcp_connections import MCPBrowseInput
from server.knowledge.app import mcp_browse as browse
from server.knowledge.app import mcp_connections, mcp_profiles
from server.knowledge.app import mcp_source_authority as authority
from tests.unit.test_mcp_source_live import live_setup as live_setup
from tests.unit.test_mcp_source_live import live_tools as live_tools
from tests.unit.test_mcp_source_live_read import protected as protected
from tests.unit.test_mcp_source_read_policy import reader as reader
from tests.unit.test_mcp_source_sync import RESOURCE, SECOND
from tests.unit.test_mcp_source_sync import harness as harness
from tests.unit.test_mcp_source_sync import upstream as upstream


@pytest.fixture
def browse_remote(protected, upstream, monkeypatch):
    # Keep the approved profile builder and official SDK; replace only its network transport.
    monkeypatch.setattr(
        mcp_connections, "build_mcp_source_client", authority.build_mcp_source_client
    )
    return protected


def browse_input(h, **changes):
    values = {"org": "org", "slot": h.account.kind, "mode": "resources"}
    return MCPBrowseInput(**(values | changes))


@pytest.mark.asyncio
async def test_resources_browse_returns_only_metadata_and_stable_hints(browse_remote):
    h = browse_remote.h
    result = await browse_remote.request.call(browse.handle_mcp_browse, browse_input(h))
    again = await browse_remote.request.call(browse.handle_mcp_browse, browse_input(h))
    assert result.supported and result.resources and result.complete and not result.truncated
    assert len(result.items) == 4
    assert [(r.id, r.uri) for r in result.items] == [(r.id, r.uri) for r in again.items]
    assert all(item.uri.startswith("mcp-resource-hint:") for item in result.items)
    assert all(set(item.model_dump()) == {"id", "title", "uri"} for item in result.items)
    assert not h.reads and not h.originals and not h.doc_rows
    assert browse_remote.request.marked
    browse_remote.imports.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_next_cursor_is_never_returned_or_reused(browse_remote, upstream):
    seen = []

    @upstream._mcp_server.list_resources()
    async def listed(request: types.ListResourcesRequest):
        assert codes_only() and not public_diagnostics()
        seen.append(request.params.cursor if request.params else None)
        return types.ListResourcesResult(
            resources=[types.Resource(uri=RESOURCE, name="private title")],
            nextCursor="private-session-token",
        )

    for _ in range(2):
        result = await browse_remote.request.call(
            browse.handle_mcp_browse, browse_input(browse_remote.h)
        )
        assert not result.complete and result.truncated and result.reason == "first_window_only"
        assert "private-session-token" not in result.model_dump_json()
        assert "cursor" not in result.model_dump()
    assert seen == [None, None] and not browse_remote.h.reads


@pytest.mark.asyncio
async def test_each_browse_restarts_without_reusing_a_changed_page(browse_remote, upstream):
    identities = iter((RESOURCE, SECOND))

    @upstream._mcp_server.list_resources()
    async def listed():
        return types.ListResourcesResult(
            resources=[types.Resource(uri=next(identities), name="private title")],
            nextCursor="session cursor",
        )

    results = [
        await browse_remote.request.call(browse.handle_mcp_browse, browse_input(browse_remote.h))
        for _ in range(2)
    ]
    assert [result.items[0].id for result in results] == [RESOURCE, SECOND]
    assert all(not result.complete and result.reason == "first_window_only" for result in results)


@pytest.mark.asyncio
async def test_oversized_remote_page_cannot_publish_partial_metadata(browse_remote, upstream):
    @upstream._mcp_server.list_resources()
    async def listed():
        return [types.Resource(uri=f"selected://document/{i}", name="title") for i in range(101)]

    with pytest.raises(ValidationError):
        await browse_remote.request.call(browse.handle_mcp_browse, browse_input(browse_remote.h))
    assert not browse_remote.request.messages and not browse_remote.h.reads


@pytest.mark.asyncio
async def test_local_limit_marks_the_window_truncated(browse_remote):
    result = await browse_remote.request.call(
        browse.handle_mcp_browse, browse_input(browse_remote.h, limit=1)
    )
    assert len(result.items) == 1 and result.truncated and not result.complete
    assert result.reason == "result_limit" and result.manual_resource_ids_supported


@pytest.mark.asyncio
async def test_unmapped_search_is_truthfully_unsupported(browse_remote):
    result = await browse_remote.request.call(
        browse.handle_mcp_browse,
        browse_input(browse_remote.h, mode="search", query="selected"),
    )
    assert not result.supported and not result.complete and not result.items
    assert result.resources is None and not result.capabilities_checked
    assert result.reason == "unsupported_mode"
    assert not browse_remote.h.reads


@pytest.mark.asyncio
async def test_explicit_mapped_search_drops_remote_body_and_uri(browse_remote, live_tools):
    result = await browse_remote.request.call(
        browse.handle_mcp_browse,
        browse_input(browse_remote.h, mode="search", query="selected"),
    )
    assert result.supported and result.mapped_search
    assert {item.id for item in result.items} == {RESOURCE, SECOND}
    assert not result.complete and result.truncated
    assert result.reason == "search_completeness_unavailable"
    assert "Search is not a read" not in result.model_dump_json()
    assert "https://untrusted.example" not in result.model_dump_json()
    assert not browse_remote.h.reads and not browse_remote.h.doc_rows


@pytest.mark.asyncio
async def test_schema_drift_refuses_mapped_search(browse_remote, live_tools):
    live_tools.schema = {**live_tools.schema, "description": "schema drift"}
    with pytest.raises(ValidationError):
        await browse_remote.request.call(
            browse.handle_mcp_browse,
            browse_input(browse_remote.h, mode="search", query="selected"),
        )
    assert not browse_remote.request.messages and not browse_remote.h.reads
    assert not browse_remote.h.account.active


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["owner", "generation", "profile", "fence", "protected"])
async def test_foreign_or_stale_connection_never_publishes(browse_remote, monkeypatch, changed):
    h = browse_remote.h
    if changed == "owner":
        current_auth_var.set(replace(browse_remote.auth, subject="other"))
    elif changed == "generation":
        h.account = replace(h.account, connection_id="a" * 32 + "/1" + "c" * 31)
    elif changed == "profile":
        monkeypatch.setattr(
            mcp_connections, "load_mcp_profile_catalog", lambda: mcp_profiles.MCPProfileCatalog()
        )
    elif changed == "fence":
        monkeypatch.setattr(authority.accounts, "_fence_current", AsyncMock(return_value=False))
    with pytest.raises(ValidationError):
        await browse_remote.request.call(
            browse.handle_mcp_browse, browse_input(h), protected=changed != "protected"
        )
    assert not browse_remote.request.messages and not h.reads


@pytest.mark.asyncio
async def test_revocation_after_listing_blocks_actual_publication(browse_remote):
    async def revoke():
        browse_remote.h.account = replace(browse_remote.h.account, active=False)

    with pytest.raises(request_tail.RequiredTailError):
        await browse_remote.request.call(
            browse.handle_mcp_browse, browse_input(browse_remote.h), before_send=revoke
        )
    assert not browse_remote.request.messages


@pytest.mark.asyncio
async def test_cancelled_listing_drains_the_owned_server_task(browse_remote, upstream):
    started, stopped = asyncio.Event(), asyncio.Event()

    @upstream._mcp_server.list_resources()
    async def blocked():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return []

    task = asyncio.create_task(
        browse_remote.request.call(browse.handle_mcp_browse, browse_input(browse_remote.h))
    )
    try:
        async with asyncio.timeout(5):
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await stopped.wait()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not browse_remote.request.messages and not browse_remote.h.reads


@pytest.mark.parametrize("extra", ["cursor", "namespace", "endpoint", "tool", "fixed_arguments"])
def test_browse_cannot_accept_continuations_or_authority_overrides(extra):
    private = "private cursor or query"
    with pytest.raises(ModelError) as error:
        MCPBrowseInput(org="org", slot="mcp:" + "a" * 32, mode="resources", **{extra: private})
    assert private not in json.dumps(error.value.errors(), default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,query", [("resources", "private query"), ("search", " ")])
async def test_mode_must_match_the_explicit_query(browse_remote, mode, query):
    with pytest.raises(ValidationError, match="MCP_BROWSE_QUERY_INVALID"):
        await browse_remote.request.call(
            browse.handle_mcp_browse, browse_input(browse_remote.h, mode=mode, query=query)
        )
    assert not browse_remote.request.messages and not browse_remote.h.reads
