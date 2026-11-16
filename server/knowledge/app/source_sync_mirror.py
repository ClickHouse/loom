"""A mirrored folder-store source's turn: every listing is a full one (no
`modified_after`), and each file is written into the namespace's mirror with the
permission list the store gives it (docs/architecture/document-sources.md, The mirror;
Every complete listing reads the lists again).

The turn first brings every stored copy's list to what the store says now: the list of
the file listed at its address, or `unread` for a copy a whole listing does not have
there. A copy behind its file's revision takes only the part of the new list its own
admitted: its text was shared under that list, and the content write widens it. A
changed list is written as a new row over the stored one that keeps its chunks, so
nothing is embedded again, and the same list writes nothing. The reads are bounded by
the pass's deadline only, never by its document bound, and what was read is written.
Then each new or changed file is fetched and written with its list, as a folder turn
writes (a mirrored copy is never converted again for the pictures its stored text holds,
as a folder turn does on a full listing), and at the full interval a copy whose file is
gone is retired on the store's word. The listing row lands once every stored copy's list
was brought up to date by a listing that searched everything."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
from datetime import UTC, datetime
from typing import Any

from server.clients import gdrive
from server.core.ch import sequential_reads
from server.core.pg_lease import PublicationPending
from server.knowledge.app import mirrors
from server.knowledge.app import source_sync as sync
from server.knowledge.domain import access_list
from server.knowledge.domain.sources import (
    KnowledgeSource,
    home,
    json_object,
    mirror_uri,
    mirror_uri_suffix,
)
from server.knowledge.store import documents
from server.knowledge.store import mirrors as mirror_store
from server.knowledge.store import sources as store
from server.workers import pass_context

log = logging.getLogger("loom.knowledge.source_sync")

# Seconds between two files' permission reads: ten a second stay well under the store's
# per-user rate limit.
PERMISSIONS_PACE_S = 0.1  # not a setting: the store's limit, the same on every deployment
# Why a listing row waits on a publication: one owed passes, one parked needs an operator.
_HELD_BACK = {
    "owed": "publication: a written list is not in ClickHouse yet",
    "parked": "publication: a written list is parked; see the knowledge delivery backlog",
}
# List rewrites read and written in one round trip each.
_REWRITE_BATCH = 100  # not a setting: a bound on one statement's size


async def turn(src: KnowledgeSource, state: dict[str, str], run: sync.Pass) -> sync.TurnResult:
    """List every file of a mirrored source, bring every stored copy's list up to date,
    then write its new and changed files and retire the gone ones."""
    detail = json_object(state.get("detail", ""))
    result = sync.TurnResult()
    # Every listing is a full one; `full` is the periodic full interval, which gives
    # each given-up file its one more try.
    result.full = bool(detail.get("full")) or sync.full_due(detail)
    sync.start_waits(result, detail)
    started = datetime.now(UTC)
    opened = await sync.open_listing(src, result, run, fields=gdrive.LISTED_WITH_PERMISSIONS)
    if opened is None:
        return result
    token, listing, files = opened
    current = functools.partial(sync.require_current, src)
    mirror = await mirrors.ensure_mirror(src.org, src.namespace, still_current=current)
    src = dataclasses.replace(src, mirror=mirror)
    if result.complete and not listing.walk_cut:
        listed_ids = {str(f["id"]) for f in files}
        result.waits = {k: v for k, v in result.waits.items() if k in listed_ids}
    # The listed files by the address their copy is stored at.
    listed = {u: f for f in files if (u := _address(src, f)).startswith("https://")}
    whole = listing.whole and not listing.lost_item
    # A head whose row ClickHouse does not have yet is not among the copies read, so one
    # owed when they are read leaves the listing vouching for nothing.
    suffix = mirror_uri_suffix(src.source_id)
    owed = await documents.publications_owed(src.org, src.mirror, suffix)
    with sequential_reads():  # a replica behind the last turn would show an older list
        copies = await store.every_copy(src.org, *home(src))
    try:
        lists_current = await _refresh_lists(src, token, copies, listed, whole, run, result)
    except _Refused:
        return result  # the store refused the account: nothing more is read or written
    stored = sync.index_with_room(result, [c for c in copies if c["searchable"]])
    try:
        for f in files:
            read_access = functools.partial(_file_access, token, f, result)
            prior = stored.get(str(f["id"]))
            uri = _address(src, f)
            if not await sync.sync_file(
                src, token, f, uri, prior, run, result, read_access=read_access
            ):
                break
    except _Refused:
        pass  # every stored list is written: the listing row may still land
    # As in a folder turn, only a full listing looks up what it no longer has: a copy
    # it does not have already reads `unread`, so waiting for the interval costs nothing.
    if result.full and result.complete:
        gone = functools.partial(sync.file_gone, token, listing=listing, turn=result)
        by_id = {str(f["id"]): uri for uri, f in listed.items()}
        await sync.retire_absent(src, by_id, gone, run, result)
    # A listing that searched everything and reached every item vouches for the lists,
    # whatever a file's content did; a walk cut at its bounds still does, since every
    # copy it did not reach is `unread`.
    if lists_current and whole:
        # Parked at the end outranks owed at the read; either holds the row back.
        held_back = await documents.publications_owed(src.org, src.mirror, suffix) or owed
        if held_back:
            note = _HELD_BACK[held_back]
            if held_back == "parked":  # no reader sees a mirror's state yet: say it here
                log.warning(
                    "knowledge_source_sync: %s/%s/%s: %s", src.org, src.namespace, src.name, note
                )
            if result.error:  # the state shows the error alone: the note goes with it
                result.error = f"{note}; {result.error}"
            else:
                _note(result, note, first=True)
            return result
        pass_context.check_stop()
        await sync.require_current(src)  # an erasure since the last write lands no row
        await mirror_store.record_listing(src.org, src.mirror, src.source_id, src.name, started)
    return result


def _address(src: KnowledgeSource, f: dict[str, Any]) -> str:
    return mirror_uri(sync.source_uri_of(f), src.source_id)


class _Refused(Exception):
    """The store refused the account a permission read (a rate limit, a refused
    credential); the turn's status says which."""


def _note(result: sync.TurnResult, note: str, *, first: bool = False) -> None:
    """Add `note` once, however many files it was for; `first` puts it ahead of the
    rest, so the state's 200-character cut keeps it."""
    if note not in result.note:
        notes = [note, result.note] if first else [result.note, note]
        result.note = "; ".join(filter(None, notes))


async def _refresh_lists(
    src: KnowledgeSource,
    token: str,
    copies: list[dict[str, str]],
    listed: dict[str, dict[str, Any]],
    whole: bool,
    run: sync.Pass,
    result: sync.TurnResult,
) -> bool:
    """Rewrite each stored copy's list to the store's, compared with the one the
    storage authority holds and written a batch at a time. False when the deadline cut
    it or a copy was not this source's to write."""
    all_written = True
    for start in range(0, len(copies), _REWRITE_BATCH):
        batch = copies[start : start + _REWRITE_BATCH]
        held = {c["source_uri"]: c["access_json"] for c in batch}
        revision = {c["source_uri"]: c["source_revision"] for c in batch}
        held |= await documents.committed_lists(src.org, src.mirror, list(held))
        changed: dict[str, access_list.AccessList] = {}
        try:
            for uri in held:
                f = listed.get(uri)
                if f is None and not whole:
                    continue  # an incomplete search may have missed it: it keeps its list
                if f is not None and "permissions" not in f and run.budget.exhausted():
                    # Past the deadline no list is read, but every one that needs no
                    # read is still compared; this one keeps its list for now.
                    result.mark(sync.TurnMark.PASS_BOUND)
                    continue
                access = access_list.UNREAD if f is None else await _file_access(token, f, result)
                stored = access_list.from_json(held[uri])
                if f is not None and revision[uri] != str(f.get("modifiedTime") or ""):
                    # The copy is behind the file: its text was shared under the old
                    # list, so only a narrowing reaches it; the content write widens.
                    access = access_list.common(stored, access)
                if stored != access:
                    changed[uri] = access
        except _Refused:
            await _write_lists(src, changed)  # what was read is written all the same
            raise
        if not await _write_lists(src, changed):
            # A listing must not vouch for a list it could not write; later batches go on.
            _note(result, "permissions: a stored copy is not this source's to write")
            all_written = False
    return all_written and not result.cut


async def _file_access(
    token: str, f: dict[str, Any], result: sync.TurnResult
) -> access_list.AccessList:
    """The file's permission list. The listing leaves it out for a shared drive's files
    and for one the account may not share, so it is read then and kept on `f`. A file
    the store will not say for, or whose read failed, reads `unread`. _Refused when the
    store refused the account."""
    if "permissions" not in f:
        pass_context.check_stop()
        try:
            f["permissions"] = await gdrive.file_permissions(token, str(f["id"]))
        except (gdrive.RateLimited, gdrive.CredentialRefused) as e:
            sync.vendor_failure(result, e)
            raise _Refused from e
        except gdrive.VendorError:
            f["permissions"] = None  # read again next turn
            _note(result, "permissions: a file's list could not be read")
        else:
            if f["permissions"] is None:  # a 403 (a Viewer account) or a file gone since
                _note(result, "permissions: the store gave no list for a file")
        await asyncio.sleep(PERMISSIONS_PACE_S)
    return access_list.from_drive_permissions(f.get("permissions"))


async def _write_lists(src: KnowledgeSource, lists: dict[str, access_list.AccessList]) -> bool:
    """Write each stored document's new list as a new row with everything else as it
    was: its chunks are kept, so nothing is embedded again. False when a document is
    no longer this source's (a guard: this turn is its addresses' only writer), so its
    list stays as it was."""
    if not lists:
        return True
    pass_context.check_stop()
    with sequential_reads():  # a replica behind the last write would carry old content
        found = await documents.get_many_by_uris(src.org, src.mirror, list(lists))
    docs = [d for d in found if sync.ours(src, d)]
    if docs:
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
    return len(docs) == len(found)


__all__ = ["PERMISSIONS_PACE_S", "turn"]
