"""Official SDK reads and real publication tails; storage and identity are unit seams."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.auth.context import current_auth_var
from server.auth.session_protection import request_declaration
from server.clients import mcp_source as client_module
from server.clients.mcp_source import MCPSourceError
from server.core import request_tail, request_work
from server.core.ch_names import qualify
from server.core.errors import ValidationError
from server.knowledge.api.models_mcp_reads import MCPReadInput
from server.knowledge.app import mcp_browse as browse
from server.knowledge.app import mcp_connections, session_admission, writes
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.store import mirror_latch
from tests.unit.test_mcp_source_read_policy import reader as reader
from tests.unit.test_mcp_source_sync import JSON_TEXT, RESOURCE, SECOND
from tests.unit.test_mcp_source_sync import harness as harness
from tests.unit.test_mcp_source_sync import upstream as upstream


class LatchStore:
    """Only fake DB responses; session admission and latch verification execute."""

    @asynccontextmanager
    async def coordination_session(self) -> AsyncIterator[None]:
        yield

    async def execute(self, *args: Any) -> None:
        pass

    async def query_one(self, *args: Any) -> dict[str, Any]:
        return {
            "server_id": "00000000-0000-4000-8000-000000000001",
            "enabled": 1,
            "engines": [
                (
                    "knowledge_mirror_latch",
                    qualify("KeeperMap('/loom/knowledge_mirror_latch') PRIMARY KEY id"),
                ),
                ("_migration_lock", qualify("KeeperMap('/loom/migration_lock') PRIMARY KEY name")),
            ],
        }


@dataclass
class Request:
    messages: list[dict[str, Any]] = field(default_factory=list)
    marked: bool = False

    async def call(self, handler, input, *, protected=True, before_send=None):
        scope = {
            "type": "http",
            "headers": [(b"x-loom-session-protection", b"v1")] if protected else [],
        }
        result = None

        async def app(scope, receive, send):
            nonlocal result
            with request_declaration(scope), request_work.bind(request_work.RequestWork()):
                result = await handler(input)
                self.marked = request_work.http_mirror_marked(scope)
                if before_send is not None:
                    await before_send()
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send(
                    {"type": "http.response.body", "body": result.model_dump_json().encode()}
                )

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            self.messages.append(message)

        with request_work.http_owner(scope):
            try:
                await request_tail.RequestTailMiddleware(app)(scope, receive, send)
            except ValidationError as error:
                if isinstance(error.__context__, MCPSourceError):
                    error.add_note("Fixed client refusal: " + error.__context__.code)
                raise
        return result


@pytest.fixture
def protected(reader, harness, monkeypatch):
    enabled = SimpleNamespace(
        auth_actually_required=lambda: True,
        knowledge_mirrored_sources_enabled=True,
        knowledge_live_search_timeout_s=10,
    )
    monkeypatch.setattr(browse, "settings", enabled)
    monkeypatch.setattr(session_admission, "settings", enabled)
    monkeypatch.setattr(mirror_latch, "get_client", LatchStore)
    monkeypatch.setattr(
        mcp_connections.accounts,
        "resolve_reader",
        AsyncMock(
            side_effect=lambda org, subject: SimpleNamespace(subject=subject, emails=frozenset())
        ),
    )
    forbidden = AsyncMock(side_effect=AssertionError("live reads cannot import"))
    monkeypatch.setattr(writes, "handle_knowledge_upsert", forbidden)
    harness.select(mode="live")
    return SimpleNamespace(h=harness, auth=reader.auth, request=Request(), imports=forbidden)


def read_input(**changes):
    values = dict(org="org", namespace="collection", name="selected", resource_id=RESOURCE)
    return MCPReadInput(**(values | changes))


@pytest.mark.asyncio
async def test_resource_only_read_is_full_selected_and_protected(protected, upstream):
    h = protected.h
    h.text = "Full selected content. " * 50
    result = await protected.request.call(browse.handle_mcp_read, read_input())
    assert result.text == h.text and len(result.text) > 200
    assert result.source_id == h.source.source_id
    assert result.uri == resource_uri(h.source.source_id, RESOURCE)
    assert result.resource_identity_verified and result.mime_type == "text/plain"
    assert not result.remote_identity_verified and not result.upstream_acl_verified
    assert protected.request.marked and len(protected.request.messages) == 2
    assert len(h.reads) >= 4 and set(h.reads) == {RESOURCE}
    assert not h.doc_rows and not h.chunk_rows and not h.originals
    protected.imports.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["tool", "text"], indirect=True)
async def test_explicit_mapped_reads_keep_receipt_identity_truthful(protected, upstream, harness):
    result = await protected.request.call(browse.handle_mcp_read, read_input())
    plain = harness.reads[0] == "tool:" + RESOURCE and result.text.startswith("Plain")
    assert result.resource_identity_verified is not plain
    assert result.uri == resource_uri(harness.source.source_id, RESOURCE)
    assert set(harness.reads) == {"tool:" + RESOURCE}
    protected.imports.assert_not_awaited()


@pytest.mark.asyncio
async def test_unprotected_read_refuses_before_any_remote_read(protected, upstream):
    with pytest.raises(ValidationError, match="MCP_PROTECTED_SESSION_REQUIRED"):
        await protected.request.call(browse.handle_mcp_read, read_input(), protected=False)
    assert not protected.h.reads and not protected.request.messages
    client_module.pinned_target.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_read_keeps_declared_text_mime(protected, upstream):
    protected.h.select(mode="live", resources=(JSON_TEXT,))
    result = await protected.request.call(browse.handle_mcp_read, read_input(resource_id=JSON_TEXT))
    assert result.mime_type == "application/json"
    assert result.text == '{"selected": "text"}'
    assert result.uri == resource_uri(protected.h.source.source_id, JSON_TEXT)


@pytest.mark.asyncio
async def test_missing_durable_session_protection_refuses_before_network(
    protected, upstream, monkeypatch
):
    original = LatchStore.query_one

    async def unlatched(self, *args):
        return {**await original(self, *args), "enabled": 0}

    monkeypatch.setattr(LatchStore, "query_one", unlatched)
    with pytest.raises(ValidationError):
        await protected.request.call(browse.handle_mcp_read, read_input())
    client_module.pinned_target.assert_not_awaited()
    assert not protected.h.reads and not protected.request.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["owner", "namespace", "unselected", "snapshot", "revoked"])
async def test_wrong_reader_or_scope_refuses_before_read(protected, upstream, monkeypatch, changed):
    values = read_input().model_dump()
    if changed == "owner":
        current_auth_var.set(replace(protected.auth, subject="other"))
    elif changed == "namespace":
        values["namespace"] = "other"
    elif changed == "unselected":
        values["resource_id"] = SECOND
    elif changed == "snapshot":
        protected.h.select(mode="snapshot")
    else:
        monkeypatch.setattr(authority.accounts, "_fence_current", AsyncMock(return_value=False))
    with pytest.raises(ValidationError):
        await protected.request.call(browse.handle_mcp_read, MCPReadInput(**values))
    assert not protected.h.reads and not protected.request.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["body", "source", "grant", "connection"])
async def test_publication_rechecks_current_body_and_authority(
    protected, upstream, monkeypatch, changed
):
    async def change():
        if changed == "body":
            protected.h.text = "changed after handler"
        elif changed == "source":
            protected.h.source = replace(protected.h.source, active=False)
        elif changed == "grant":
            monkeypatch.setattr(authority.authz, "row_in_scope", AsyncMock(return_value=False))
        else:
            protected.h.account = replace(protected.h.account, active=False)

    with pytest.raises(request_tail.RequiredTailError):
        await protected.request.call(browse.handle_mcp_read, read_input(), before_send=change)
    assert not protected.request.messages
    protected.imports.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_read_drains_owned_asgi_work(protected, upstream):
    started, stopped = asyncio.Event(), asyncio.Event()

    @upstream._mcp_server.read_resource()
    async def blocked(uri):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return []

    task = asyncio.create_task(protected.request.call(browse.handle_mcp_read, read_input()))
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
    assert not protected.request.messages and not protected.h.doc_rows


@pytest.mark.parametrize("extra", ["endpoint", "source_id", "slot", "uri", "tool"])
def test_read_validation_does_not_echo_private_inputs(extra):
    from pydantic import ValidationError as ModelError

    secret = "private selected identity"
    with pytest.raises(ModelError) as error:
        MCPReadInput(
            org="org",
            namespace="collection",
            name="selected",
            resource_id=secret,
            **{extra: "private override"},
        )
    assert secret not in json.dumps(error.value.errors(), default=str)
