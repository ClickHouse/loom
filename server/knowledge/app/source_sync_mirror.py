"""A mirrored folder-store source's turn: every listing is complete, and each file is
written into the namespace's mirror with the permission list the store gives it
(docs/architecture/document-sources.md, The mirror; Every complete listing reads the
lists again).

The turn first brings every stored copy's list to what the store says now: the list of
the file listed at its address, or `unread` for a copy the listing does not have there.
A changed list is written as a new row over the stored one that keeps its chunks, so
nothing is embedded again, and the same list writes nothing. The reads are bounded by
the pass's deadline only, never by its document bound, and what was read is written.
Then each new or changed file is fetched and written with its list, as a folder turn
writes, and a copy whose file is gone is retired on the store's word. The listing row
lands once every stored copy's list was brought up to date by a listing that searched
everything."""

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
# List rewrites read and written in one round trip each.
_REWRITE_BATCH = 100  # not a setting: a bound on one statement's size
# The statuses a refused permission read leaves, which stop the turn.
_REFUSED = ("rate_limited", "credential_invalid")


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
        sync.vendor_failure(result, e)
        return result
    sync.note_listing(result, listing)
    if listing.ran_out:
        return result
    if not await sync.items_visible(token, src.items, result, run.budget.exhausted):
        return result
    files = [f for f in listing.files if gdrive.valid_file_id(str(f.get("id") or ""))]
    result.documents_seen = len(files)
    if result.complete and not listing.walk_cut:
        listed_ids = {str(f["id"]) for f in files}
        result.waits = {k: v for k, v in result.waits.items() if k in listed_ids}
    addressed = [(mirror_uri(sync.source_uri_of(f), src.source_id), f) for f in files]
    listed = {uri: f for uri, f in addressed if uri.startswith("https://")}
    with sequential_reads():  # a replica behind the last turn would show an older list
        copies = await store.source_revisions(
            src.org, *home(src), searchable_only=False, flag_searchable=True
        )
    lists_current = await _refresh_lists(src, token, copies, listed, run, result)
    if result.status in _REFUSED:
        return result  # the store refused a permission read: no fetch follows
    stored = sync.with_room(result, [c for c in copies if c["searchable"]])
    for uri, f in addressed:
        if run.budget.exhausted():
            result.mark(sync.TurnMark.PASS_BOUND)
            break
        ready = functools.partial(_read_permissions, token, f, result)
        access = functools.partial(_access_of, f)
        prior = stored.get(str(f["id"]))
        if not await sync.sync_file(
            src, token, f, uri, prior, run, result, ready=ready, access=access
        ):
            break
        if result.status in _REFUSED:
            break  # a permission read the store refused
    if result.complete:
        gone = functools.partial(sync.file_gone, token, listing=listing, turn=result)
        ids = {str(f["id"]): uri for uri, f in listed.items()}
        await sync.retire_absent(src, ids, gone, run, result)
    # A listing that searched everything and reached every item vouches for the lists,
    # whatever a file's content did; a walk cut at its bounds still does, since every
    # copy it did not reach is `unread`.
    namespace, _ = home(src)
    if lists_current and listing.whole and not listing.lost_item:
        # A head whose row ClickHouse does not have yet was not among the copies read.
        if await documents.publications_owed(src.org, namespace):
            return result
        pass_context.check_stop()
        await mirrors.record_listing(src.org, namespace, src.source_id, src.name, started)
    return result


async def _mirror(src: KnowledgeSource) -> str:
    """The namespace's mirror, made under the namespace's sources lease only when it has
    none, so a turn takes the lease once per mirror."""
    if mirror := await mirrors.live_mirror(src.org, src.namespace):
        return mirror
    async with source_ops.sources_lease(src.org, src.namespace):
        return await mirrors.ensure_mirror(src.org, src.namespace)


async def _refresh_lists(
    src: KnowledgeSource,
    token: str,
    copies: list[dict[str, str]],
    listed: dict[str, dict[str, Any]],
    run: sync.Pass,
    result: sync.TurnResult,
) -> bool:
    """Rewrite every stored copy's list to what the store says now: the list of the file
    `listed` at its address, or `unread`. Each list is compared with the one the storage
    authority holds, and changed lists are written a batch at a time as they are read,
    so a turn the deadline cuts keeps what it read. True when every copy now has a list
    read this turn, or `unread`."""
    namespace, _ = home(src)
    for start in range(0, len(copies), _REWRITE_BATCH):
        batch = copies[start : start + _REWRITE_BATCH]
        held = {c["source_uri"]: c["access_json"] for c in batch}
        held |= await documents.committed_lists(src.org, namespace, list(held))
        changed: dict[str, access_list.AccessList] = {}
        read_all = True
        for uri in held:
            if run.budget.exhausted():
                result.mark(sync.TurnMark.PASS_BOUND)
                read_all = False
                break
            f = listed.get(uri)
            if f is not None and not await _read_permissions(token, f, result):
                read_all = False
                break
            access = access_list.UNREAD if f is None else _access_of(f)
            if access_list.from_json(held[uri]) != access:
                changed[uri] = access
        await _write_lists(src, changed)
        if not read_all:
            return False
    return True


def _access_of(f: dict[str, Any]) -> access_list.AccessList:
    return access_list.from_drive_permissions(f.get("permissions"))


async def _read_permissions(token: str, f: dict[str, Any], result: sync.TurnResult) -> bool:
    """Read the file's permissions when the listing left them out (a shared drive's
    files, or one the account may not share). A file the store will not say for, or
    whose read failed, reads `unread`. False when the store refused the account and the
    turn stops."""
    if "permissions" in f:
        return True
    pass_context.check_stop()
    try:
        f["permissions"] = await gdrive.file_permissions(token, str(f["id"]))
    except (gdrive.RateLimited, gdrive.CredentialRefused) as e:
        sync.vendor_failure(result, e)
        return False
    except gdrive.VendorError:
        f["permissions"] = None  # read again next turn
        result.mark(sync.TurnMark.LIST_UNREAD)
    await asyncio.sleep(PERMISSIONS_PACE_S)
    return True


async def _write_lists(src: KnowledgeSource, lists: dict[str, access_list.AccessList]) -> None:
    """Write each stored document's new list as a new row with everything else as it
    was: its chunks are kept, so nothing is embedded again."""
    if not lists:
        return
    namespace, _ = home(src)
    pass_context.check_stop()
    with sequential_reads():  # a replica behind the last write would carry old content
        docs = await documents.get_many_by_uris(src.org, namespace, list(lists))
    docs = [d for d in docs if sync.ours(src, d)]
    if not docs:
        return
    await sync.require_current(src)
    rows = [
        documents.build_document_row(
            d,
            version=documents.version_past(d.version),
            access_json=access_list.to_json(lists[d.source_uri]),
        )
        for d in docs
    ]
    with contextlib.suppress(PublicationPending):  # committed; the worker publishes it
        await documents.insert_many(rows, over={d.source_uri: d.version for d in docs})


__all__ = ["PERMISSIONS_PACE_S", "turn"]
