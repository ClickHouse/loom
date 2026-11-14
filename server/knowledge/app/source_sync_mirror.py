"""A mirrored folder-store source's turn: every listing is complete, and each file is
written into the namespace's mirror with the permission list the store gives it
(docs/architecture/document-sources.md, The mirror; Every complete listing reads the
lists again).

A file at a new revision is fetched and written with its list. At the same revision only
a changed list is written, as a new row over the stored one that keeps its chunks, so
nothing is embedded again. A stored file the complete listing no longer has is written
`unread` first and then retired on the store's word, as a folder turn retires. The
listing row lands last, once every list the listing read is written."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
from datetime import UTC, datetime
from typing import Any

from server.clients import gdrive
from server.core.pg_lease import PublicationPending
from server.knowledge.app import mirrors
from server.knowledge.app import source_gdrive_items as gdrive_items
from server.knowledge.app import source_sync as sync
from server.knowledge.app import sources as source_ops
from server.knowledge.domain import access_list
from server.knowledge.domain.sources import (
    KnowledgeSource,
    home,
    include_in,
    json_object,
    mirror_uri,
)
from server.knowledge.store import documents
from server.workers import pass_context


async def turn(src: KnowledgeSource, state: dict[str, str], run: sync.Pass) -> sync.TurnResult:
    detail = json_object(state.get("detail", ""))
    result = sync.TurnResult()
    result.full = True
    sync.start_waits(result, detail)
    credential = source_ops.credential_of(src)
    if not credential:
        # Fail closed: a credential that no longer opens is never read as "no auth".
        result.fail("credential_invalid", "the stored credential no longer opens; rotate it")
        return result
    async with source_ops.sources_lease(src.org, src.namespace):
        src = dataclasses.replace(src, mirror=await mirrors.ensure_mirror(src.org, src.namespace))
    started = datetime.now(UTC)
    try:
        token = await gdrive.access_token(gdrive.parse_service_account(credential))
        listing = await gdrive_items.list_items(
            token,
            src.items,
            include_in(src.config),
            fields=gdrive.LISTED_WITH_PERMISSIONS,
            out_of_time=run.budget.exhausted,
        )
        if not listing.ran_out:
            await _read_missing_permissions(token, listing.files)
    except gdrive.RateLimited:
        result.fail("rate_limited", "rate_limited")
        return result
    except gdrive.CredentialRefused as e:
        result.fail("credential_invalid", sync.client_error(e))
        return result
    except gdrive.VendorError as e:
        result.fail("failed", sync.client_error(e))
        return result
    sync.note_listing(result, listing)
    if listing.ran_out:
        return result
    if not await sync.items_visible(token, src.items, result, run.budget.exhausted):
        return result
    files = [f for f in listing.files if gdrive.valid_file_id(str(f.get("id") or ""))]
    result.documents_seen = len(files)
    stored = await sync.stored_copies_with_room(src, result)
    addresses = {str(f["id"]): mirror_uri(sync.source_uri_of(f), src.source_id) for f in files}
    listed_all = True
    for f in files:
        uri = addresses[str(f["id"])]
        prior = stored.get(str(f["id"]))
        if _unchanged(f, uri, prior):
            await _relist(src, uri, f, prior)
            continue
        if not await sync.sync_file(src, token, f, uri, prior, run, result):
            listed_all = False
            break
    trusted = result.complete and not listing.walk_cut
    if trusted:
        await _unread_absent(src, stored, addresses)
        gone = functools.partial(sync.file_gone, token, listing=listing, turn=result)
        listed = {k: v for k, v in addresses.items() if v.startswith("https://")}
        await sync.retire_absent(src, listed, gone, run, result)
    if trusted and listed_all and result.status == "ok":
        namespace, _ = home(src)
        await mirrors.record_listing(src.org, namespace, src.source_id, src.name, started)
    return result


async def _read_missing_permissions(token: str, files: list[dict[str, Any]]) -> None:
    """The permissions of each file the listing left them out of (a shared drive's, or
    one the account may not share), read one file at a time; a file the store will not
    say for keeps none, and reads `unread`."""
    for f in files:
        if "permissions" not in f and gdrive.valid_file_id(str(f.get("id") or "")):
            pass_context.check_stop()
            f["permissions"] = await gdrive.file_permissions(token, str(f["id"]))


def _unchanged(f: dict[str, Any], uri: str, prior: dict[str, str] | None) -> bool:
    """Whether the stored copy is this file at this revision and address."""
    return bool(
        prior
        and prior["source_revision"] == str(f.get("modifiedTime") or "")
        and prior["source_uri"] == uri
    )


async def _relist(
    src: KnowledgeSource, uri: str, f: dict[str, Any], prior: dict[str, str] | None
) -> None:
    """Write the file's list over its stored copy when it changed; the same list writes
    nothing."""
    access = access_list.from_drive_permissions(f.get("permissions"))
    if prior is not None and access_list.from_json(prior["access_json"]) != access:
        await _rewrite_list(src, uri, access)


async def _unread_absent(
    src: KnowledgeSource, stored: dict[str, dict[str, str]], addresses: dict[str, str]
) -> None:
    """Write `unread` over each stored copy the listing no longer has, before it is
    retired, so a copy the store cannot yet say is gone admits nobody meanwhile."""
    for file_id, prior in stored.items():
        if addresses.get(file_id) != prior["source_uri"] and prior["access_json"]:
            await _rewrite_list(src, prior["source_uri"], access_list.UNREAD)


async def _rewrite_list(src: KnowledgeSource, uri: str, access: access_list.AccessList) -> None:
    """A new row of the stored document with `access` and everything else as it was: its
    chunks are kept, so nothing is embedded again."""
    namespace, _ = home(src)
    pass_context.check_stop()
    doc = await documents.get_one(src.org, namespace, uri, include_superseded=False)
    if doc is None or not sync.ours(src, doc):
        return
    await sync.require_current(src)
    row = documents.build_document_row(
        doc,
        version=documents.version_past(doc.version),
        access_json=access_list.to_json(access),
    )
    with contextlib.suppress(PublicationPending):  # committed; the worker publishes it
        await documents.insert_one(row, over=doc.version)


__all__ = ["turn"]
