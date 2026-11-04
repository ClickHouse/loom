"""A page store source's turn of the sync pass (`source_sync`): its listed pages, the
pages of its listed databases and the child pages found in them, each written as
one document.

The pass, its bounds, the waits and the state it records are `source_sync`'s; each
listed page goes through `sync.sync_item`. A turn lists every page on its first
turn, when asked and once a day; otherwise each listed page, each known child page
and the database pages edited after the stored stamp. A page whose stored copy has
the listed revision (its last edit time) and was read at least 90 s after it is
skipped without a fetch; one read sooner is read and written again with its new read
time, so it is skipped once that is past the margin. A changed page's blocks are
read and turned into Markdown (`convert.page_markdown`). The child pages a read page
holds are recorded in the sync state and listed on every later turn, so a child page
is a document of its own; a child found during a turn is synced in the same turn. A
known child the store reports gone is forgotten only by a complete full turn.

A listed page or a known child that is in the trash is left out of the listing, so
a full turn's lookup retires its document. A listed page, database or data source
the integration no longer sees, or a database in the trash, fails the turn and
retires nothing, while the pages it still sees are written. A document is retired
only after a complete full listing lacks its page and a lookup of that page finds
it gone, in the trash, or under none of the listed pages, listed databases and
known children.
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from server.clients import notion
from server.core.errors import ValidationError
from server.core.hashing import sha256_text
from server.core.secrets_at_rest import unseal
from server.knowledge.app import source_sync as sync
from server.knowledge.domain import convert
from server.knowledge.domain.sources import KnowledgeSource, json_object
from server.knowledge.store import sources as store
from server.settings import settings

log = logging.getLogger("loom.knowledge.source_sync")

# Requests one page's blocks may take: about two minutes at the store's pace, so one
# page cannot hold a pass. A page past it is given up as too large.
_MAX_BLOCK_REQUESTS = 300  # not a setting: a bound on one page's share of a pass
# Child pages one source keeps track of. Each is read once a turn for its revision,
# so this many take about 70 s of a turn at the store's pace; more are left out of
# their parent and counted in its `skipped_blocks`.
_MAX_CHILD_PAGES = 200  # not a setting: a bound on one source's share of a pass
# How long after a page's revision a read of it is final: the store's edit stamps
# may be coarser than its edits, and its clock may differ from ours, so a page read
# sooner is read again.
_SETTLE_S = 90.0  # not a setting: a margin on the store's stamp resolution and clock skew


def _now() -> datetime:
    return datetime.now(UTC)


class _TurnStop(Exception):
    """A rate limit or a refused token while listing: the turn's, not one page's."""

    def __init__(self, error: notion.NotionError) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass
class _Turn:
    """One turn's source, token and result, and the pages it handles."""

    src: KnowledgeSource
    token: str
    result: sync.TurnResult
    # Known child pages: child page id -> the id of the page it was found on. A child
    # is forgotten in three ways only: its page is read and no longer holds it; a
    # complete full turn's lookup finds it gone (and retires its document); or a
    # complete full turn read it as not found or trashed and it holds no document.
    known: dict[str, str] = field(default_factory=dict)
    # The pages this turn handles, in order; a child found during the turn is appended.
    items: list[dict[str, Any]] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)
    # Known children whose read this turn answered not found or trashed.
    unanswered: set[str] = field(default_factory=set)

    def add(self, page: dict[str, Any]) -> None:
        item = _item(page)
        if notion.valid_id(item["id"]) and item["id"] not in self.ids:
            self.items.append(item)
            self.ids.add(item["id"])


async def turn(src: KnowledgeSource, state: dict[str, str], run: sync.Pass) -> sync.TurnResult:
    """List, fetch, convert and write one source's changed pages; retire those a
    complete full listing no longer has."""
    detail = json_object(state.get("detail", ""))
    stamp = state.get("state_value", "")
    result = sync.TurnResult()
    result.full = bool(detail.get("full")) or not sync.valid_stamp(stamp) or sync.full_due(detail)
    sync.start_waits(result, detail)
    token = unseal("knowledge_sources.credential", src.credential_enc)
    if not token:
        # Fail closed: a credential that no longer opens is never read as "no auth".
        result.fail("credential_invalid", "the stored credential no longer opens; rotate it")
        return result
    stored_children = detail.get("child_pages")
    known = {
        str(k): str(v)
        for k, v in (stored_children if isinstance(stored_children, dict) else {}).items()
        if notion.valid_id(str(k))
    }
    t = _Turn(src, token, result, known=known)
    result.kind_state["child_pages"] = t.known
    listed_at = _now()
    try:
        await _list(t, "" if result.full else stamp)
    except _TurnStop as stop:
        _fail_turn(result, stop.error)
        return result
    # Only a complete listing of containers the integration still sees says what is absent.
    trusted = result.full and result.complete
    if trusted:
        _drop_out_of_scope(t)
        result.waits = {k: v for k, v in result.waits.items() if k in t.ids}
    # The stamp trails the listing by the settle margin too, so a database page read
    # within the margin is listed again by the next incremental turn.
    newest = max((f["modifiedTime"] for f in t.items), default="")
    result.newest = sync.capped(newest, listed_at - timedelta(seconds=_SETTLE_S))
    stored = {
        d["file_id"]: d for d in await store.source_revisions(src.org, src.namespace, src.name)
    }
    # Changed pages first: a pass bound spent on settling re-reads delays no edit.
    t.items.sort(key=lambda f: not _changed(f, stored.get(f["id"])))
    for f in t.items:  # a child found while syncing is appended and handled too
        if not await _sync_page(t, f, stored.get(f["id"]), run):
            break
    result.documents_seen = len(t.items)
    if trusted and result.complete:
        addresses = {f["id"]: sync.source_uri_of(f) for f in t.items}
        listed = {k: v for k, v in addresses.items() if v.startswith("https://")}

        gone = functools.partial(_page_gone, t)
        await sync.retire_absent(src, listed, gone, run, result)
        if result.complete:
            await _forget_unlisted_without_documents(t)
    return result


async def _forget_unlisted_without_documents(t: _Turn) -> None:
    """After a complete full turn: forget the known children whose read this turn
    answered not found or trashed and that hold no document (never written, or
    retired), so a child gone for good costs no read each turn."""
    src = t.src
    held = {
        d["file_id"]
        for d in await store.source_revisions(
            src.org, src.namespace, src.name, searchable_only=False
        )
    }
    for child_id in list(t.known):
        if child_id in t.unanswered and child_id not in held:
            t.known.pop(child_id)


def _changed(f: dict[str, Any], stored: dict[str, str] | None) -> bool:
    """Whether a listed page differs from its stored copy: none stored, another
    revision or another address."""
    if stored is None:
        return True
    return stored["source_revision"] != f["modifiedTime"] or stored["source_uri"] != (
        sync.source_uri_of(f)
    )


def _item(page: dict[str, Any]) -> dict[str, Any]:
    """A listed page in the shape the shared turn helpers read, with its parent."""
    parent = page.get("parent") or {}
    return {
        "id": str(page.get("id") or ""),
        "name": notion.title_of(page),
        "modifiedTime": str(page.get("last_edited_time") or ""),
        "webViewLink": str(page.get("url") or ""),
        "parent_page": str(parent.get("page_id") or ""),
        # A page under a block (a column, a toggle, a list item, a synced block) names
        # the block, not the page that holds it.
        "parent_block": str(parent.get("block_id") or ""),
        "parent_database": str(parent.get("database_id") or ""),
    }


def _fail_turn(result: sync.TurnResult, e: notion.NotionError) -> None:
    if isinstance(e, notion.RateLimited):
        result.fail("rate_limited", "rate_limited")
    else:
        result.fail("credential_invalid", sync.client_error(e))


def _container_failed(result: sync.TurnResult, e: notion.NotionError) -> None:
    """A listed page, database or data source, or a known child, that could not be
    read: the turn fails and retires nothing, but goes on with the others. A rate
    limit or a 401 stops it."""
    if isinstance(e, notion.RateLimited) or e.status == 401:
        raise _TurnStop(e)
    result.fail("failed", sync.client_error(e))


async def _list(t: _Turn, edited_after: str) -> None:
    """The listed pages, the pages of the listed databases (only those edited after
    `edited_after` when it is set) and the known child pages. A page in the trash or
    a child the store no longer has is left out, so a full listing's lookup retires
    its document."""
    config = t.src.config
    for page_id in config.get("page_ids") or []:
        try:
            page = await notion.page(t.token, page_id)
        except notion.NotionError as e:
            _container_failed(t.result, e)
            continue
        if not page.get("in_trash"):
            t.add(page)
    for database_id in config.get("database_ids") or []:
        try:
            db = await notion.database(t.token, database_id)
        except notion.NotionError as e:
            _container_failed(t.result, e)
            continue
        if db.get("in_trash"):
            # Its pages are not removed one by one, so nothing says which to retire.
            t.result.fail("failed", f"database {database_id}: in the trash")
            continue
        for data_source_id in notion.data_source_ids(db):
            try:
                found, whole = await notion.query_pages(
                    t.token, data_source_id, edited_after=edited_after
                )
            except notion.NotionError as e:
                _container_failed(t.result, e)
                continue
            for page in found:
                if not page.get("in_trash"):
                    t.add(page)
            if not whole:
                # Write what it listed, but keep the stamp and retire nothing.
                t.result.complete = False
                t.result.note = "listing: incomplete result"
    for child_id in list(t.known):
        try:
            page = await notion.page(t.token, child_id)
        except notion.NotFound:
            t.unanswered.add(child_id)  # absent: a full turn's lookup decides
            continue
        except notion.NotionError as e:
            _container_failed(t.result, e)
            continue
        if page.get("in_trash"):
            t.unanswered.add(child_id)
        else:
            t.add(page)


def _holder(t: _Turn, f: dict[str, Any]) -> str:
    """The page a child page sits in: its parent page, or for a page under a block,
    the page whose blocks held it when that page was last read."""
    if f["parent_block"]:
        return t.known.get(f["id"], "")
    return str(f["parent_page"])


def _drop_out_of_scope(t: _Turn) -> None:
    """After a complete full listing: leave out the known children no longer under a
    listed page, a listed database's page or another such child (a child moved
    elsewhere), so their documents count as absent and the lookup decides."""
    in_scope = {f["id"] for f in t.items if f["id"] not in t.known}
    children = [f for f in t.items if f["id"] in t.known]
    grew = True
    while grew:
        grew = False
        for f in children:
            if f["id"] not in in_scope and _holder(t, f) in in_scope:
                in_scope.add(f["id"])
                grew = True
    t.items = [f for f in t.items if f["id"] in in_scope]
    t.ids = in_scope


async def _page_gone(t: _Turn, page_id: str) -> bool | None:
    """Whether the store says the page is gone, as `_lookup` answers; a known child
    found gone is forgotten."""
    gone = await _lookup(t, page_id)
    if gone:
        t.known.pop(page_id, None)
    return gone


async def _lookup(t: _Turn, page_id: str) -> bool | None:
    """Whether the store says the page is gone: not found, in the trash, or under
    none of the listed pages, listed databases and pages this turn listed. A lookup
    that fails retires nothing; a rate limit or a 401 is the turn's, and None stops
    the retiring."""
    try:
        page = await notion.page(t.token, page_id)
    except notion.NotFound:
        return True
    except notion.NotionError as e:
        if isinstance(e, notion.RateLimited) or e.status == 401:
            _fail_turn(t.result, e)
            return None
        return False
    if page.get("in_trash"):
        return True
    item = _item(page)
    if page_id in (t.src.config.get("page_ids") or []) or item["parent_database"] in (
        t.src.config.get("database_ids") or []
    ):
        return False
    holder = _holder(t, item)
    if holder not in t.ids:
        return True
    # Still under a page this turn listed, but not known (left out at the cap, or
    # dropped when its page was read without it): known again, so it is synced.
    if page_id not in t.known and len(t.known) < _MAX_CHILD_PAGES:
        t.known[page_id] = holder
    return False


def _settled(revision: str, read_at: str) -> bool:
    """Whether a copy read at `read_at` holds every edit stamped `revision`."""
    try:
        edited, read = datetime.fromisoformat(revision), datetime.fromisoformat(read_at)
    except ValueError:
        return False
    return (read - edited).total_seconds() >= _SETTLE_S


async def _sync_page(
    t: _Turn, f: dict[str, Any], prior: dict[str, str] | None, run: sync.Pass
) -> bool:
    """Handle one listed page; False when the turn must stop listing."""
    uri = sync.source_uri_of(f)
    if (
        prior
        and prior["source_uri"] == uri
        and not _settled(f["modifiedTime"], prior.get("read_at", ""))
    ):
        # Read too soon after its revision: a later edit may share the stamp, so the
        # page is read and written again, with the new read time; once that is past
        # the margin, the stored copy lets the page be skipped.
        prior = None
    fetch_and_write = functools.partial(_fetch_and_write, t, f, uri)
    if not await sync.sync_item(t.src, f, uri, prior, run, t.result, fetch_and_write):
        return False
    # A rate limit or a refused token while listing the page's new children.
    return t.result.status not in ("rate_limited", "credential_invalid")


async def _fetch_and_write(t: _Turn, f: dict[str, Any], uri: str) -> str:
    """Read one page's blocks, record its child pages, convert and write them:
    `written`, `waiting`, `given_up` or `stop`, as `sync.sync_item` expects."""
    result = t.result
    read_at = _now()
    try:
        blocks = await notion.page_blocks(t.token, f["id"], max_requests=_MAX_BLOCK_REQUESTS)
        content, skipped = convert.page_markdown(blocks)
    except notion.RateLimited:
        result.fail("rate_limited", "rate_limited")
        return "stop"
    except notion.TooLarge:
        return sync.give_up(f, result, "too_large")
    except notion.NotionError as e:
        if e.status == 401:
            result.fail("credential_invalid", sync.client_error(e))
            return "stop"
        if e.refused:
            return sync.give_up(f, result, f"http_{e.status}")
        reason = f"http_{e.status}" if e.status else "unreachable"
        return sync.wait(f, result, reason, sync.client_error(e))
    except Exception as e:  # noqa: BLE001 — one page's conversion must not stop the others
        log.warning("knowledge_source_sync: reading %s failed: %s", f["id"], e)
        return sync.wait(f, result, f"fetch_failed:{type(e).__name__}", type(e).__name__)
    found = _record_children(t, f["id"], convert.child_page_ids(blocks), skipped)
    if not content.strip():
        outcome = sync.give_up(f, result, "no_text")
    elif len(content.encode()) > int(settings.knowledge_source_sync_max_file_bytes):
        outcome = sync.give_up(f, result, "too_large")
    else:
        outcome = await _write(t, f, uri, content, skipped, read_at)
    # A rate limit or a refused token here ends the turn after this page's outcome,
    # so a written page's old address is still retired (`_sync_page`).
    await _add_new_children(t, found)
    return outcome


def _exact_hash(f: dict[str, Any], content: str, skipped: dict[str, int]) -> str:
    """A hash of exactly what a write stores for the page: its title, its Markdown as
    written (whitespace included) and the blocks left out. The mining step keys the
    document by it, so a copy written again with the same text is not mined again."""
    left_out = json.dumps(skipped, sort_keys=True)
    return sha256_text(f"{f['name']}\n{content}\n{left_out}")


async def _write(
    t: _Turn,
    f: dict[str, Any],
    uri: str,
    content: str,
    skipped: dict[str, int],
    read_at: datetime,
) -> str:
    await sync.require_current(t.src)
    metadata: dict[str, Any] = {
        "read_at": sync.stamp_of(read_at),
        "text_sha256": _exact_hash(f, content, skipped),
    }
    if skipped:
        metadata["skipped_blocks"] = skipped
    try:
        await sync.write_document(
            t.src, f, uri, content, ingested_format="blocks", extra_metadata=metadata
        )
    except ValidationError as e:
        # The write refuses this page whatever the pass: it will not go through.
        code = e.to_detail().get("code") or "invalid"
        return sync.give_up(f, t.result, f"write_refused:{code}")
    except Exception as e:  # noqa: BLE001 — one page's write must not stop the others
        log.warning("knowledge_source_sync: writing %s failed: %s", f["id"], e)
        return sync.wait(f, t.result, f"write_failed:{type(e).__name__}", type(e).__name__)
    t.result.changed += 1
    return "written"


def _record_children(
    t: _Turn, parent: str, children: list[str], skipped: dict[str, int]
) -> list[str]:
    """Record the child pages a read page holds, replacing what it held before; the
    ones past `_MAX_CHILD_PAGES` are counted in its `skipped_blocks`. Returns the
    recorded ones this turn has not listed."""
    for child, held_by in list(t.known.items()):
        if held_by == parent and child not in children:
            t.known.pop(child)
    listed_pages = set(t.src.config.get("page_ids") or [])
    over = 0
    for child in children:
        if child in listed_pages or not notion.valid_id(child):
            continue
        if child not in t.known and len(t.known) >= _MAX_CHILD_PAGES:
            over += 1
            continue
        t.known[child] = parent
    if over:
        skipped["child_page"] = skipped.get("child_page", 0) + over
    return [c for c in children if c in t.known and c not in t.ids]


async def _add_new_children(t: _Turn, children: list[str]) -> None:
    """List the children a read page holds that this turn has not, so they are synced
    in this turn. A rate limit or a refused token ends the turn (its status says so).
    A child that cannot be read now stays known and is read next turn; any failure
    other than not found leaves the turn incomplete, so a failed or cut turn forgets no
    child it could not read."""
    for child in children:
        try:
            page = await notion.page(t.token, child)
        except notion.NotFound:
            t.unanswered.add(child)
            continue
        except notion.NotionError as e:
            if isinstance(e, notion.RateLimited) or e.status == 401:
                _fail_turn(t.result, e)
                return
            t.result.fail("failed", sync.client_error(e))
            continue
        if page.get("in_trash"):
            t.unanswered.add(child)
        else:
            t.add(page)


__all__ = ["turn"]
