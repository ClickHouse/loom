"""A mirrored folder-store source's turn: every listing is complete, and each file is
written into the namespace's mirror with the permission list the store gives it
(docs/architecture/document-sources.md, The mirror; Every complete listing reads the
lists again).

A file's list is read with the listing, or one file at a time when the listing leaves
it out. A stored copy at the file's address takes the list the store gives it now,
written as a new row over the stored one that keeps its chunks, so nothing is embedded
again; the same list writes nothing. A file at a new revision is then fetched and
written with its list. A stored copy the complete listing no longer has at its address
is written `unread` first and then retired on the store's word, as a folder turn
retires. The listing row lands last, once every list the listing read is written."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
from datetime import UTC, datetime
from typing import Any

from server.clients import gdrive
from server.core.ch import sequential_reads
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
from server.knowledge.store import sources as store
from server.workers import pass_context

# Seconds between two files' permission reads: ten a second stay well under the store's
# per-user rate limit.
PERMISSIONS_PACE_S = 0.1  # not a setting: the store's limit, the same on every deployment


async def turn(src: KnowledgeSource, state: dict[str, str], run: sync.Pass) -> sync.TurnResult:
    detail = json_object(state.get("detail", ""))
    result = sync.TurnResult()
    # Every listing is complete; `full` is the periodic full interval, which gives each
    # given-up file its one more try.
    result.full = bool(detail.get("full")) or sync.full_due(detail)
    sync.start_waits(result, detail)
    credential = source_ops.credential_of(src)
    if not credential:
        # Fail closed: a credential that no longer opens is never read as "no auth".
        result.fail("credential_invalid", "the stored credential no longer opens; rotate it")
        return result
    src = dataclasses.replace(src, mirror=await _mirror(src))
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
    except gdrive.VendorError as e:
        _fail(result, e)
        return result
    sync.note_listing(result, listing)
    if listing.ran_out:
        return result
    if not await sync.items_visible(token, src.items, result, run.budget.exhausted):
        return result
    files = [f for f in listing.files if gdrive.valid_file_id(str(f.get("id") or ""))]
    result.documents_seen = len(files)
    trusted = result.complete and not listing.walk_cut
    if trusted:
        result.waits = {k: v for k, v in result.waits.items() if k in {str(f["id"]) for f in files}}
    stored = await sync.stored_copies_with_room(src, result)
    addresses = {str(f["id"]): mirror_uri(sync.source_uri_of(f), src.source_id) for f in files}
    for f in files:
        if run.budget.exhausted():
            result.mark(sync.TurnMark.PASS_BOUND)
            break
        if not await _read_permissions(token, f, result):
            break
        uri = addresses[str(f["id"])]
        prior = stored.get(str(f["id"]))
        if prior is not None and prior["source_uri"] == uri:
            # Even a file whose new revision is not written (too large, given up)
            # keeps the list the store gives it now.
            await _relist(src, uri, f, prior)
            if prior["source_revision"] == str(f.get("modifiedTime") or ""):
                continue
        if not await sync.sync_file(src, token, f, uri, prior, run, result):
            break
    if result.complete and not listing.walk_cut:
        await _unread_absent(src, addresses, run, result)
        gone = functools.partial(sync.file_gone, token, listing=listing, turn=result)
        listed = {k: v for k, v in addresses.items() if v.startswith("https://")}
        await sync.retire_absent(src, listed, gone, run, result)
    if result.complete and not listing.walk_cut and result.status == "ok":
        namespace, _ = home(src)
        await mirrors.record_listing(src.org, namespace, src.source_id, src.name, started)
    return result


async def _mirror(src: KnowledgeSource) -> str:
    """The namespace's mirror, made under the namespace's sources lease only when it has
    none, so a turn takes the lease once per mirror."""
    if mirror := await mirrors.live_mirror(src.org, src.namespace):
        return mirror
    async with source_ops.sources_lease(src.org, src.namespace):
        return await mirrors.ensure_mirror(src.org, src.namespace)


def _fail(result: sync.TurnResult, e: gdrive.VendorError) -> None:
    if isinstance(e, gdrive.RateLimited):
        result.fail("rate_limited", "rate_limited")
    elif isinstance(e, gdrive.CredentialRefused):
        result.fail("credential_invalid", sync.client_error(e))
    else:
        result.fail("failed", sync.client_error(e))


async def _read_permissions(token: str, f: dict[str, Any], result: sync.TurnResult) -> bool:
    """Read the file's permissions when the listing left them out (a shared drive's
    files, or one the account may not share); a file the store will not say for reads
    `unread`. False when the store refused and the turn stops."""
    if "permissions" in f:
        return True
    pass_context.check_stop()
    try:
        f["permissions"] = await gdrive.file_permissions(token, str(f["id"]))
    except gdrive.VendorError as e:
        _fail(result, e)
        return False
    await asyncio.sleep(PERMISSIONS_PACE_S)
    return True


async def _relist(src: KnowledgeSource, uri: str, f: dict[str, Any], prior: dict[str, str]) -> None:
    """Write the file's list over its stored copy when it changed; the same list writes
    nothing."""
    access = access_list.from_drive_permissions(f.get("permissions"))
    if access_list.from_json(prior["access_json"]) != access:
        await _rewrite_list(src, uri, access)


async def _unread_absent(
    src: KnowledgeSource, addresses: dict[str, str], run: sync.Pass, result: sync.TurnResult
) -> None:
    """Write `unread` over each stored copy the listing no longer has at its address,
    before it is retired, so a copy the store cannot yet say is gone admits nobody
    meanwhile."""
    for doc in await store.source_revisions(src.org, *home(src), searchable_only=False):
        if addresses.get(doc["file_id"]) == doc["source_uri"] or not doc["access_json"]:
            continue
        if run.budget.exhausted():
            result.mark(sync.TurnMark.PASS_BOUND)
            return
        await _rewrite_list(src, doc["source_uri"], access_list.UNREAD)


async def _rewrite_list(src: KnowledgeSource, uri: str, access: access_list.AccessList) -> None:
    """A new row of the stored document with `access` and everything else as it was: its
    chunks are kept, so nothing is embedded again."""
    namespace, _ = home(src)
    pass_context.check_stop()
    with sequential_reads():  # a replica behind the last write would carry old content
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


__all__ = ["PERMISSIONS_PACE_S", "turn"]
