"""Bounded refresh of an explicit selected MCP resource set, without retirement."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from server.clients.mcp_source import MCPEvidence, MCPSourceClient, MCPSourceError
from server.core.ch import sequential_reads, set_query_tag
from server.core.hashing import sha256_document_text
from server.core.pg_lease import PublicationPending
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import mirrors, private_sync
from server.knowledge.app import source_sync as sync
from server.knowledge.app.mcp_read_receipt import require_read_receipt
from server.knowledge.app.mcp_source_sessions import checked_session
from server.knowledge.domain.access_list import UNREAD, AccessList, to_json
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.domain.sources import KnowledgeSource, mirror_uri_suffix
from server.knowledge.store import documents
from server.knowledge.store import mirrors as mirror_store
from server.workers import pass_context
from server.workers.status import set_worker_identity


def _detail(state: dict[str, str]) -> dict[str, Any]:
    try:
        value = json.loads(state.get("detail", "") or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _progress(
    bound: authority.SourceAuthority, saved: dict[str, Any], asked: dict[str, Any]
) -> dict[str, Any]:
    count = len(bound.contract.resources)
    index = saved.get("mcp_next_index", 0)
    request = asked.get("requested_at", saved.get("mcp_requested_at", ""))
    if (
        saved.get("mcp_scope_sha256") != bound.scope_sha256
        or saved.get("mcp_requested_at", "") != request
        or type(index) is not int
        or not 0 <= index <= count
    ):
        index = 0
    if index == count and bound.contract.mode == "selected_refresh":
        index = 0
    return {
        "mcp_mode": bound.contract.mode,
        "mcp_scope_sha256": bound.scope_sha256,
        "mcp_next_index": index,
        "mcp_selected_count": count,
        "mcp_requested_at": request,
        "mcp_selected_complete": False,
    }


async def _owned_head(
    bound: authority.SourceAuthority, uri: str, selected: str
) -> tuple[documents.DocumentRow, dict[str, Any]] | None:
    src = bound.source
    if uri not in bound.selected_uris:
        return None
    with sequential_reads():
        rows = await documents.get_many_by_uris(src.org, src.mirror, [uri])
    if len(rows) != 1:
        return None
    doc = rows[0]
    if (
        (doc.org, doc.namespace, doc.source_uri) != (src.org, src.mirror, uri)
        or doc.superseded_at is not None
        or not sync.ours(src, doc)
    ):
        return None
    try:
        metadata = json.loads(doc.metadata)
    except (ValueError, TypeError):
        return None
    if not isinstance(metadata, dict) or (
        metadata.get("source_kind"),
        metadata.get("source_name"),
        metadata.get("mcp_source_id"),
    ) != ("mcp", src.name, src.source_id):
        return None
    origin = uri if bound.contract.reader_policy == "collection_copy" else selected
    if (
        metadata.get("ingested_from") != origin
        or metadata.get("ingested_format") != "mcp_text"
        or metadata.get("mcp_reader_policy") != bound.contract.reader_policy
    ):
        return None
    return doc, metadata


async def _replace_head(
    bound: authority.SourceAuthority,
    doc: documents.DocumentRow,
    metadata: dict[str, Any],
    access: AccessList,
) -> bool:
    row = documents.build_document_row(
        doc,
        version=documents.version_past(doc.version),
        metadata=json.dumps(metadata, ensure_ascii=False),
        access_json=to_json(access),
    )
    await bound.validate()
    pass_context.check_stop()
    with suppress(PublicationPending):  # committed; the worker publishes it
        count = await documents.insert_many([row], over={doc.source_uri: doc.version})
        return count == 1 and not await documents.publications_owed(
            doc.org, doc.namespace, doc.source_uri
        )
    return False


async def _refresh(
    bound: authority.SourceAuthority,
    uri: str,
    evidence: MCPEvidence,
    metadata: dict[str, Any],
    access: AccessList,
) -> bool | None:
    async with authority.publication_guard(bound.source):
        if await documents.publications_owed(bound.source.org, bound.source.mirror, uri):
            return False
        head = await _owned_head(bound, uri, evidence.resource_id)
        if head is None:
            return None
        doc, previous = head
        if (
            doc.title != evidence.title
            or doc.content != evidence.text
            or doc.content_sha256 != sha256_document_text(f"{doc.title}\n{doc.content}")
            or previous.get("mcp_content_sha256") != metadata["mcp_content_sha256"]
            or not doc.blob_path
            or doc.blob_erased_at is not None
            or doc.blob_size != len(evidence.text.encode())
            or doc.blob_content_type.partition(";")[0].strip().lower() != "text/plain"
            or doc.revision < 1
        ):
            return None
        return await _replace_head(bound, doc, {**previous, **metadata}, access)


async def _invalidate(bound: authority.SourceAuthority, selected: str) -> None:
    src = bound.source
    uri = resource_uri(src.source_id, selected)
    async with authority.publication_guard(src):
        if await documents.publications_owed(src.org, src.mirror, uri):
            return
        head = await _owned_head(bound, uri, selected)
        if head is None:
            return
        doc, metadata = head
        metadata.pop("mcp_read_at", None)
        metadata.pop("mcp_scope_sha256", None)
        await _replace_head(bound, doc, metadata, UNREAD)


async def _publish(
    bound: authority.SourceAuthority,
    client: MCPSourceClient,
    selected: str,
    evidence: MCPEvidence,
    read_at: datetime,
) -> bool:
    digest = require_read_receipt(client, selected, evidence)
    src = bound.source
    copied = bound.contract.reader_policy == "collection_copy"
    uri = resource_uri(src.source_id, selected)
    access = AccessList(anyone=True) if copied else AccessList(owner=src.owner_subject)
    metadata = {
        "mcp_content_sha256": digest,
        "mcp_scope_sha256": bound.scope_sha256,
        "mcp_config_sha256": client.config_sha256,
        "mcp_profile_sha256": bound.contract.profile_sha256,
        "mcp_connection_generation": bound.contract.generation,
        "mcp_source_id": src.source_id,
        "mcp_read_operation": "tools/call" if client.config.read else "resources/read",
        "mcp_reader_policy": bound.contract.reader_policy,
        "mcp_read_at": read_at.isoformat(),
        "mcp_identity_verified": evidence.receipt.identity_verified if evidence.receipt else False,
        "mcp_declared_mime_type": evidence.mime_type,
        **({"ingested_from": uri} if copied else {}),
    }
    refreshed = await _refresh(bound, uri, evidence, metadata, access)
    if refreshed is not None:
        return refreshed
    written = await sync.write_document(
        src,
        {"id": selected, "name": evidence.title, "mimeType": "text/plain; charset=utf-8"},
        uri,
        evidence.text,
        ingested_format="mcp_text",
        original=evidence.text.encode(),
        permissions=access,
        extra_metadata=metadata,
    )
    return written and not await documents.publications_owed(src.org, src.mirror, uri)


async def _read_selected(
    bound: authority.SourceAuthority, result: sync.TurnResult, run: sync.Pass
) -> None:
    stored = await sync.stored_copies_with_room(bound.source, result)
    progress = result.kind_state
    client = bound.client()
    async with checked_session(bound.connection.account, client):
        await client.discover()
        for index in range(progress["mcp_next_index"], len(bound.contract.resources)):
            set_query_tag("worker." + sync.WORKER)
            if run.spent():
                result.mark(sync.TurnMark.PASS_BOUND)
                return
            selected = bound.contract.resources[index].id
            if selected not in stored and result.room <= 0:
                result.held += 1
                result.fail("failed", "MCP_DOCUMENT_LIMIT")
                return
            run.left -= 1
            try:
                evidence = await client.read(selected)
            except MCPSourceError as error:
                if error.code != "schema":
                    await _invalidate(bound, selected)
                raise
            read_at = datetime.now(UTC)
            result.documents_seen += 1
            if run.budget.exhausted():
                result.mark(sync.TurnMark.PASS_BOUND)
                return
            await bound.validate()
            pass_context.check_stop()
            if not await _publish(bound, client, selected, evidence, read_at):
                result.fail("failed", "MCP_DOCUMENT_HELD")
                return
            result.changed += 1
            if selected not in stored:
                result.room -= 1
            progress["mcp_next_index"] = index + 1


async def _finish(
    bound: authority.SourceAuthority, result: sync.TurnResult, run: sync.Pass, started: datetime
) -> None:
    src = bound.source
    if result.status != "ok" or result.marks:
        return
    if run.budget.exhausted():
        result.mark(sync.TurnMark.PASS_BOUND)
        return
    if await documents.publications_owed(src.org, src.mirror, mirror_uri_suffix(src.source_id)):
        result.fail("failed", "MCP_PUBLICATION_PENDING")
        return
    async with authority.publication_guard(src):
        await mirror_store.record_listing(src.org, src.mirror, src.source_id, src.name, started)
    result.kind_state["mcp_selected_complete"] = True
    result.complete = True


async def _turn(
    src: KnowledgeSource, state: dict[str, str], run: sync.Pass, result: sync.TurnResult
) -> None:
    bound = await authority.validate_source(src)
    if bound.contract.mode == "live":
        result.kind_state = {"mcp_mode": "live", "mcp_selected_complete": False}
        result.complete = True
        return
    async with accounts.account_lease(src.org, src.owner_subject, bound.contract.slot):
        mirror = await mirrors.ensure_mirror(src.org, src.namespace, still_current=bound.validate)
    src = replace(src, mirror=mirror)
    bound = await authority.validate_source(src)
    result.mirror = mirror
    saved = _detail(await sync.mirror_state(src, mirror))
    result.kind_state = _progress(bound, saved, _detail(state))
    if (
        bound.contract.mode == "snapshot"
        and result.kind_state["mcp_next_index"] == len(bound.contract.resources)
        and saved.get("mcp_selected_complete") is True
    ):
        result.kind_state["mcp_selected_complete"] = True
        result.complete = True
        return
    started = datetime.now(UTC)
    async with private_sync.mirror_costs(src.org, mirror, sync.WORKER):
        set_worker_identity(sync.WORKER, org=src.org, namespace=mirror)
        await _read_selected(bound, result, run)
        await _finish(bound, result, run, started)


async def turn(src: KnowledgeSource, state: dict[str, str], run: sync.Pass) -> sync.TurnResult:
    """Refresh only stored selections; completion never asserts upstream enumeration."""
    result = sync.TurnResult(complete=False)
    with private_sync.scope():
        try:
            if run.spent():
                result.mark(sync.TurnMark.PASS_BOUND)
                return result
            async with asyncio.timeout(max(0.0, run.budget.stop_after - time.monotonic())):
                await _turn(src, state, run, result)
        except TimeoutError:
            result.mark(sync.TurnMark.PASS_BOUND)
        except authority.MCPAuthorityError:
            result.fail("unavailable", "MCP_SOURCE_UNAVAILABLE")
        except MCPSourceError as error:
            result.fail("failed", "MCP_READ_" + error.code.upper())
    return result
