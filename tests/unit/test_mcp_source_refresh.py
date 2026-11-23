"""Per-document MCP refresh through real SDK and document row builders."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import source_sync_mcp as engine
from server.knowledge.app import writes
from server.knowledge.app.mcp_read_receipt import require_read_receipt
from server.knowledge.domain import access_list
from server.knowledge.store import documents
from server.operations.base import Role
from tests.unit.test_mcp_source_sync import (
    JSON_TEXT,
    MIRROR,
    RESOURCE,
    SECOND,
    Harness,
    execute,
)
from tests.unit.test_mcp_source_sync import (
    harness as harness,
)
from tests.unit.test_mcp_source_sync import (
    storage as storage,
)
from tests.unit.test_mcp_source_sync import (
    upstream as upstream,
)


@pytest.fixture
def heads(storage: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    def latest(*args: Any, **kwargs: Any) -> list[documents.DocumentRow]:
        by_uri = {row["source_uri"]: row for row in storage.doc_rows}
        return [documents.DocumentRow(**by_uri[uri]) for uri in args[2] if uri in by_uri]

    monkeypatch.setattr(documents, "get_many_by_uris", AsyncMock(side_effect=latest))
    monkeypatch.setattr(
        documents,
        "get_one",
        AsyncMock(side_effect=lambda *a, **k: next(iter(latest(*a[:2], [a[2]])), None)),
    )
    return storage


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["owner_only", "collection_copy"])
async def test_unchanged_refresh_preserves_every_non_access_field(
    heads: Harness, upstream: FastMCP, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    h = heads
    h.select(mode="selected_refresh", policy=policy)
    if policy == "collection_copy":
        monkeypatch.setattr(authority.authz, "grant_role_for", AsyncMock(return_value=Role.ADMIN))
    await execute(h)
    before = dict(h.doc_rows[-1])
    before.update(agent="author-agent", user_id="author", session_id="author-session")
    h.doc_rows[-1] = before
    embed = AsyncMock(side_effect=AssertionError("unchanged content must not embed"))
    monkeypatch.setattr(writes, "_embed_inline_batch", embed)
    earliest = datetime.now(UTC)
    result = await execute(h)
    assert result.complete and result.status == "ok"
    after = h.doc_rows[-1]
    assert len(h.doc_rows) == 2 and len(h.chunk_rows) == len(h.originals) == 1
    assert {k: v for k, v in before.items() if k not in {"metadata", "access_json", "version"}} == {
        k: v for k, v in after.items() if k not in {"metadata", "access_json", "version"}
    }
    assert after["version"] > before["version"]
    metadata = json.loads(after["metadata"])
    bound = await authority.validate_source(replace(h.source, mirror=MIRROR))
    assert metadata["mcp_scope_sha256"] == bound.scope_sha256
    assert earliest <= datetime.fromisoformat(metadata["mcp_read_at"]) <= datetime.now(UTC)
    assert metadata["mcp_identity_verified"] is True
    assert metadata["mcp_config_sha256"]
    expected = (
        access_list.AccessList(anyone=True)
        if policy == "collection_copy"
        else (access_list.AccessList(owner="owner"))
    )
    assert access_list.from_json(after["access_json"]) == expected
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_text_uses_actual_upsert(heads: Harness, upstream: FastMCP) -> None:
    heads.select(mode="selected_refresh")
    await execute(heads)
    heads.text = "Changed selected text."
    result = await execute(heads)
    assert result.complete and len(heads.originals) == len(heads.chunk_rows) == 2
    assert heads.doc_rows[-1]["content"] == heads.text
    assert heads.doc_rows[-1]["revision"] == heads.doc_rows[0]["revision"] + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("blob_path", ""),
        ("blob_size", 1),
        ("content_sha256", "wrong"),
        ("title", "Changed title"),
        ("metadata", "{}"),
    ],
)
async def test_unsafe_original_or_head_uses_upsert(
    heads: Harness, upstream: FastMCP, field: str, value: Any
) -> None:
    heads.select(mode="selected_refresh")
    await execute(heads)
    heads.doc_rows[-1][field] = value
    result = await execute(heads)
    assert result.complete and len(heads.originals) == len(heads.chunk_rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["source", "account"])
async def test_late_authority_loss_prevents_refresh(
    heads: Harness, upstream: FastMCP, monkeypatch: pytest.MonkeyPatch, late: str
) -> None:
    heads.select(mode="selected_refresh")
    await execute(heads)
    read = documents.get_many_by_uris

    async def replaced(*args: Any, **kwargs: Any) -> Any:
        row = await read(*args, **kwargs)
        if late == "source":
            heads.source = replace(heads.source, active=False)
        else:
            heads.account = replace(heads.account, active=False)
        return row

    monkeypatch.setattr(documents, "get_many_by_uris", replaced)
    result = await execute(heads)
    assert result.status == "unavailable" and not result.complete
    assert len(heads.doc_rows) == len(heads.originals) == len(heads.chunk_rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["owner_only", "collection_copy"])
async def test_failed_item_clears_only_its_proof(
    heads: Harness, upstream: FastMCP, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    heads.select(mode="selected_refresh", policy=policy, two=True)
    if policy == "collection_copy":
        monkeypatch.setattr(authority.authz, "grant_role_for", AsyncMock(return_value=Role.ADMIN))
    await execute(heads)
    old = dict(heads.doc_rows[-1])
    heads.failing_resource = SECOND
    result = await execute(heads)
    assert result.status == "failed" and not result.complete and result.removed == 0
    assert result.kind_state["mcp_next_index"] == 1
    fresh, failed = heads.doc_rows[-2:]
    assert json.loads(fresh["metadata"])["mcp_read_at"]
    expected = (
        access_list.AccessList(anyone=True)
        if policy == "collection_copy"
        else access_list.AccessList(owner="owner")
    )
    assert access_list.from_json(fresh["access_json"]) == expected
    metadata = json.loads(failed["metadata"])
    assert "mcp_read_at" not in metadata and "mcp_scope_sha256" not in metadata
    assert access_list.from_json(failed["access_json"]) == access_list.UNREAD
    assert {k: v for k, v in failed.items() if k not in {"metadata", "access_json", "version"}} == {
        k: v for k, v in old.items() if k not in {"metadata", "access_json", "version"}
    }
    assert len(heads.originals) == len(heads.chunk_rows) == 2
    heads.listing.assert_awaited_once()
    heads.failing_resource = ""
    heads.saved = result.kind_state
    retry = await execute(heads)
    assert retry.complete and json.loads(heads.doc_rows[-1]["metadata"])["mcp_read_at"]
    assert len(heads.originals) == len(heads.chunk_rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["text"], indirect=True)
async def test_plain_sdk_tool_receipt_is_honest(heads: Harness, upstream: FastMCP) -> None:
    result = await execute(heads)
    assert result.complete and heads.originals == ["Plain selected text. λ".encode()]
    metadata = json.loads(heads.doc_rows[-1]["metadata"])
    assert metadata["mcp_identity_verified"] is False
    assert metadata["mcp_read_operation"] == "tools/call"
    assert metadata["mcp_read_at"] and metadata["mcp_scope_sha256"]


@pytest.mark.asyncio
async def test_sdk_text_resource_with_json_mime_is_text(heads: Harness, upstream: FastMCP) -> None:
    heads.select(resources=(JSON_TEXT,))
    result = await execute(heads)
    assert result.complete and heads.originals == [b'{"selected": "text"}']
    document = heads.doc_rows[-1]
    assert document["blob_content_type"] == "text/plain; charset=utf-8"
    assert json.loads(document["metadata"])["mcp_declared_mime_type"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_pending_head_never_counts_as_success(
    heads: Harness, upstream: FastMCP, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    heads.select(mode="selected_refresh")
    if existing:
        await execute(heads)
    monkeypatch.setattr(
        documents,
        "publications_owed",
        AsyncMock(side_effect=lambda *a: "owed" if heads.doc_rows else ""),
    )
    result = await execute(heads)
    assert result.status == "failed" and not result.complete and result.changed == 0
    assert len(heads.doc_rows) == len(heads.originals) == len(heads.chunk_rows) == 1


@pytest.mark.asyncio
async def test_refresh_uses_exact_optimistic_version(
    heads: Harness, upstream: FastMCP, monkeypatch: pytest.MonkeyPatch
) -> None:
    heads.select(mode="selected_refresh")
    await execute(heads)
    before = heads.doc_rows[-1]
    insert = AsyncMock(return_value=0)
    monkeypatch.setattr(documents, "insert_many", insert)
    result = await execute(heads)
    assert result.status == "failed" and not result.complete
    assert insert.await_args.kwargs == {"over": {before["source_uri"]: before["version"]}}
    assert len(heads.doc_rows) == len(heads.originals) == len(heads.chunk_rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"actor": "foreign"}, {"tags": []}])
async def test_foreign_head_is_never_refreshed(
    heads: Harness, upstream: FastMCP, change: dict[str, Any]
) -> None:
    heads.select(mode="selected_refresh")
    await execute(heads)
    heads.doc_rows[-1].update(change)
    result = await execute(heads)
    assert result.status == "failed" and not result.complete
    assert len(heads.doc_rows) == len(heads.originals) == len(heads.chunk_rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("unexpected"), asyncio.CancelledError()])
async def test_non_remote_failures_never_invalidate(
    heads: Harness, upstream: FastMCP, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    heads.select(mode="selected_refresh")
    await execute(heads)
    monkeypatch.setattr(engine.MCPSourceClient, "read", AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await execute(heads)
    assert len(heads.doc_rows) == 1
    assert json.loads(heads.doc_rows[-1]["metadata"])["mcp_read_at"]


@pytest.mark.asyncio
async def test_remote_failure_cannot_bypass_lost_authority(
    heads: Harness, upstream: FastMCP, monkeypatch: pytest.MonkeyPatch
) -> None:
    heads.select(mode="selected_refresh")
    await execute(heads)

    async def failed(*args: Any) -> None:
        heads.source = replace(heads.source, active=False)
        raise engine.MCPSourceError("protocol")

    monkeypatch.setattr(engine.MCPSourceClient, "read", failed)
    result = await execute(heads)
    assert result.status == "unavailable" and not result.complete
    assert len(heads.doc_rows) == len(heads.originals) == len(heads.chunk_rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["text"], indirect=True)
async def test_plain_receipt_still_binds_body_request_config_and_operation(
    harness: Harness, upstream: FastMCP
) -> None:
    bound = await authority.validate_source(harness.source)
    client = bound.client()
    async with client:
        evidence = await client.read(RESOURCE)
        assert evidence.receipt is not None and evidence.receipt.identity_verified is False
        assert require_read_receipt(client, RESOURCE, evidence) == evidence.receipt.content_sha256
        for changed in (
            replace(evidence, text="tampered"),
            replace(evidence, resource_id=SECOND),
            replace(evidence, receipt=None),
            replace(evidence, receipt=replace(evidence.receipt, requested_id=SECOND)),
            replace(evidence, receipt=replace(evidence.receipt, config_sha256="0" * 64)),
            replace(evidence, receipt=replace(evidence.receipt, operation="resources/read")),
        ):
            with pytest.raises(engine.MCPSourceError):
                require_read_receipt(client, RESOURCE, changed)
