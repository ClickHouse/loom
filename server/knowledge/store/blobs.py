"""loom.document_blobs: the uploaded originals behind `loom.documents.blob_path`.

A document's `blob_path` is the sha256 of its original's bytes; one catalog row
per (org, namespace, sha256) says where the bytes are, so re-uploading the same
file writes a new version of that key's row (with `s3`, naming a new object) and
two documents with the same bytes share it. With
`document_object_store=clickhouse` the bytes are the row's `data`; with `s3`
they are an object in the bucket that the row's `location` names
(`server/core/object_store.py`), and `data` is empty. A row is read from
wherever it says, whatever the setting is now.

`created_at` is the row's version, so a re-upload restamps it. Outside erasure
only the sweep (`server/knowledge/workers/blob_gc.py`) deletes an original, and
only one unwritten for `document_blob_min_age_seconds`: a write stores its
original before the document row that names it, so a check that finds no
document naming a fresh original may be racing that write.

Every S3 write is a new object, written before the row that names it; an object
no row names (a replaced row's, a write whose row never landed) is the sweep's
once it is older than the same age.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from server.core import object_store as objects
from server.core.ch import sequential_reads
from server.core.ch_params import chunks
from server.core.repo_base import _CHClient, _default_client
from server.erasure.domain.scopes import parse_namespace_list
from server.knowledge.store.documents import LIVE_ORIGINAL_SQL
from server.knowledge.store.ingestions import HELD_SQL
from server.settings import DOCUMENT_UPLOAD_BUDGET_S, settings

log = logging.getLogger(__name__)

TABLE = "loom.document_blobs"
UPLOADS = "loom.blob_uploads"
_NS = "org = {org:String} AND namespace = {ns:String}"
_KEY = f"{_NS} AND sha256 = {{sha:String}}"
_IN_KEYS = "(org, namespace, sha256) IN {keys:Array(Tuple(String, String, String))}"

# Rows written before a cutoff read from the ClickHouse clock and bound: a replicated
# table refuses now64() in a mutation.
_OLDER_THAN_CUTOFF = "created_at < fromUnixTimestamp64Milli({cutoff_ms:Int64})"


async def _cutoff_ms(min_age_s: int, ch: _CHClient) -> int:
    """Now on the ClickHouse clock, less `min_age_s`, in epoch milliseconds."""
    row = await ch.query_one(
        "SELECT toUnixTimestamp64Milli(now64(3)) - {age:UInt32} * 1000 AS cutoff_ms",
        params={"age": int(min_age_s)},
    )
    if row is None:
        raise RuntimeError("SELECT without FROM returned no row")
    return int(row["cutoff_ms"])


async def put(
    org: str, namespace: str, data: bytes, *, uploader: str = "-", ch: _CHClient | None = None
) -> str:
    """Store `data` and return its sha256, the document's `blob_path`. Committed
    before it returns: the document row that names it is written next. An end
    user's upload is recorded first (`loom.blob_uploads`), so their erasure
    finds every original they stored; a write the store then fails keeps that
    record (no content), which a concurrent upload of the same bytes may need. With
    the S3 store the object is written before its catalog row, so a row never names
    an object that is not there, under a transfer slot held for the object alone;
    one slower than DOCUMENT_UPLOAD_BUDGET_S is refused and its object, which no
    row names, left to the bucket walk."""
    sha = hashlib.sha256(data).hexdigest()
    ch = ch or _default_client()
    row: dict[str, Any] = {"org": org, "namespace": namespace, "sha256": sha, "size": len(data)}
    # Before anything is written: a key too long for the sweep's cursor
    # (`objects.MAX_KEY`) refuses the upload.
    loc = (
        objects.new_location(org, namespace, sha)
        if settings.document_object_store == "s3"
        else None
    )
    await _record_upload(org, namespace, sha, uploader, ch)
    if loc is None:
        row.update(data=data, location="")
    else:
        async with objects.transfer() as lease:
            started = time.monotonic()
            await objects.put(loc, data, sha, lease)
            took = time.monotonic() - started
        if took > DOCUMENT_UPLOAD_BUDGET_S:
            log.warning("blobs: an upload took longer than its budget; %s is left", loc.uri())
            raise objects.ObjectStoreError(
                f"object store write took longer than {DOCUMENT_UPLOAD_BUDGET_S} s",
                operation="write",
            )
        row.update(data=b"", location=loc.uri())
    await ch.insert(TABLE, [row], settings={"async_insert": 0})
    return sha


async def _record_upload(org: str, namespace: str, sha: str, uploader: str, ch: _CHClient) -> None:
    """Who uploaded the original, for their erasure; none for a service's write."""
    if uploader not in ("", "-"):
        await ch.insert(
            UPLOADS,
            [{"org": org, "namespace": namespace, "sha256": sha, "user_id": uploader}],
            settings={"async_insert": 0},
        )


@dataclass(frozen=True)
class Stored:
    """Where the latest row of an original says its bytes are."""

    size: int
    location: str  # '' for the row's `data`, else the object's `s3://` location


async def locate(
    org: str, namespace: str, sha: str, *, ch: _CHClient | None = None
) -> Stored | None:
    """The original's size and location, or None when no row holds it. Reads no data."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT size, location FROM {TABLE} WHERE {_KEY} ORDER BY created_at DESC LIMIT 1",
        params={"org": org, "ns": namespace, "sha": sha},
    )
    return Stored(int(row["size"]), str(row["location"])) if row else None


async def read(
    org: str, namespace: str, sha: str, *, stored: Stored, ch: Any = None
) -> bytes | None:
    """The original's bytes, buffered; None when no row or object holds them
    whole (an object deleted after `stored` was read included), so a missing
    original is not a 0-byte one. On the ClickHouse store a 0-byte original whose
    row went after `stored` was read reads as empty. `stored` is what `locate`
    answered."""
    ch = ch or _default_client()
    if stored.location:
        found = await objects.read(objects.Location.parse(stored.location))
        return found if found is not None and len(found) == stored.size else None
    # Any row of the ClickHouse store: every version of a sha holds the same bytes,
    # so no sort reads each unmerged version's data to pick one.
    data: bytes = await ch.raw_bytes(
        f"SELECT data FROM {TABLE} WHERE {_KEY} AND location = '' LIMIT 1 FORMAT RawBLOB",
        {"org": org, "ns": namespace, "sha": sha},
    )
    return data if len(data) == stored.size else None


@asynccontextmanager
async def stream(
    org: str, namespace: str, sha: str, *, stored: Stored, ch: Any = None
) -> AsyncIterator[AsyncIterator[bytes]]:
    """The original's bytes as chunks, straight off the HTTP response; empty
    when no row or object holds it. `stored` is what `locate` answered."""
    ch = ch or _default_client()
    if stored.location:
        parts = objects.stream(objects.Location.parse(stored.location))
        try:
            yield parts
        finally:
            await parts.aclose()
        return
    async with ch.raw_stream(
        f"SELECT data FROM {TABLE} WHERE {_KEY} AND location = '' LIMIT 1 FORMAT RawBLOB",
        {"org": org, "ns": namespace, "sha": sha},
    ) as raw:
        yield raw


@dataclass(frozen=True)
class Held:
    """An original whose latest catalog row holds its bytes in ClickHouse."""

    org: str
    namespace: str
    sha: str
    size: int


@dataclass(frozen=True)
class HeldPage:
    held: list[Held]  # of the page's keys, those whose latest row is the ClickHouse store's
    after: str  # the sha the next page starts after; '' past the namespace's last key


async def held_in_clickhouse(
    org: str, namespace: str, after: str, limit: int, *, ch: _CHClient | None = None
) -> HeldPage:
    """One page of the namespace's originals: the keys of the next `limit` rows after
    the sha `after` (a key with versions not yet merged takes a row for each), and of
    them those whose latest row holds its bytes in ClickHouse, the ones a move to the
    bucket has left. Each page ends past `after`, so a walk always moves on. Reads no
    data.

    The page's last key is read first, in key order on one thread, so a page reads
    about its own rows however many the namespace holds (an aggregate over the rest of
    the namespace with a LIMIT reads all of it); the held ones are then aggregated
    within that key range."""
    ch = ch or _default_client()
    scope = {"org": org, "ns": namespace, "after": after}
    last = await ch.query_one(
        f"SELECT count() AS n, toString(max(sha256)) AS last FROM ("
        f"SELECT sha256 FROM {TABLE} WHERE {_NS} AND sha256 > {{after:String}} "
        "ORDER BY sha256 LIMIT {lim:UInt32}) "
        "SETTINGS max_threads = 1",
        params={**scope, "lim": int(limit)},
    )
    if not last or not int(last["n"]):
        return HeldPage([], "")
    upper = str(last["last"])
    rows = await ch.query_all(
        f"SELECT toString(sha256) AS sha, argMax(size, created_at) AS size FROM {TABLE} "
        f"WHERE {_NS} AND sha256 > {{after:String}} AND sha256 <= {{upper:String}} "
        "GROUP BY sha256 HAVING argMax(location, created_at) = '' ORDER BY sha256",
        params={**scope, "upper": upper},
    )
    held = [Held(org, namespace, str(r["sha"]), int(r["size"])) for r in rows]
    return HeldPage(held, upper)


async def needed(
    org: str, namespace: str, shas: list[str], *, ch: _CHClient | None = None
) -> set[str]:
    """Of `shas`, the originals the namespace still needs: named by a live document (as
    its original or its extraction artifact), or held by an unexpired upload or an open
    ingestion. The rest are the sweep's (`unreferenced`). One read for a page of the
    walk."""
    ch = ch or _default_client()
    if not shas:
        return set()
    with sequential_reads():
        rows = await ch.query_all(
            "SELECT sha FROM ("
            " SELECT arrayJoin([blob_path, artifact_sha256]) AS sha FROM loom.documents FINAL"
            f" WHERE {_NS} AND {LIVE_ORIGINAL_SQL}"
            "  AND (blob_path IN {shas:Array(String)} OR artifact_sha256 IN {shas:Array(String)})"
            f" UNION DISTINCT SELECT sha FROM ({HELD_SQL})"
            ") WHERE sha IN {shas:Array(String)}",
            params={"org": org, "ns": namespace, "shas": shas},
        )
    return {str(r["sha"]) for r in rows}


class CorruptOriginal(Exception):
    """An original's bytes do not hash to its sha256 or do not fill its size, in
    ClickHouse or read back from the bucket: nothing is switched."""


class ErasureOpen(Exception):
    """An erasure that covers the original has not finished, or ran while it was
    copied and kept it: it stays in ClickHouse. The message names the erasure."""


class SwitchedUnderErasure(Exception):
    """The original was switched to the bucket as an erasure of one of its users
    started, and that erasure has not finished: what it decides for the original
    holds for the switched row too. The message names it."""


# How far an erasure's own timestamps (the server's clock, whole seconds) may sit
# from the ClickHouse clock a move reads, and still count as overlapping it.
_CLOCK_MARGIN_S = 60  # not a setting: a bound on clock skew, answered conservatively
# How long a move waits for an erasure that started while it moved, and how often it
# reads the ledger meanwhile.
ERASURE_WAIT_S = 600  # not a setting: a one-off operator command's patience
_ERASURE_POLL_S = 1.0  # not a setting: a one-off operator command's poll cadence

# The erasures of an org that had not finished by a moment: open, failed, or completed
# after it.
_ERASURES_SINCE = """
SELECT toString(id) AS erasure_id, scope_type, scope_value, toString(status) AS state
FROM loom.erasure_requests FINAL
WHERE org = {org:String}
  AND (status != 'completed'
       OR completed_at >= fromUnixTimestamp({since:Int64}) - INTERVAL {margin:UInt32} SECOND)
"""


@dataclass(frozen=True)
class _Erasure:
    id: str
    scope_type: str
    status: str

    @property
    def done(self) -> bool:
        return self.status == "completed"

    def __str__(self) -> str:
        return f"erasure {self.id} ({self.scope_type}, {self.status})"


def _covers(scope_type: str, scope_value: str, namespace: str) -> bool:
    """Whether an erasure of this scope may erase an original of `namespace`: its
    namespace's, a list naming it, and any user's. A user's erasure finds the user's
    originals through their upload records and their documents, which a write can add
    to at any moment, so the move does not guess whose an original is."""
    if scope_type == "namespace":
        return scope_value == namespace
    if scope_type == "namespace_list":
        try:
            return namespace in parse_namespace_list(scope_value)
        except ValueError:
            return True  # unreadable: answered as covering
    return True


def _fence_advice(erasures: list[_Erasure], what: str) -> str:
    """What holds a move back, named by the erasure that holds it longest (a failed one,
    else an unfinished one, else one that finished just now), and what to do."""
    failed = [e for e in erasures if e.status == "failed"]
    if failed:
        return (
            f"{failed[0]} {what}; complete it with loom erasure-re-verify --resume, then run again"
        )
    open_ = [e for e in erasures if not e.done]
    if open_:
        return f"{open_[0]} {what}; run again once it has finished"
    return f"{erasures[0]} {what} and finished just now; run again in a minute"


async def _erasures_since(org: str, namespace: str, since_s: int, ch: _CHClient) -> list[_Erasure]:
    """The erasures that cover the original and had not finished at `since_s`."""
    with sequential_reads():
        rows = await ch.query_all(
            _ERASURES_SINCE, params={"org": org, "since": since_s, "margin": _CLOCK_MARGIN_S}
        )
    return [
        _Erasure(str(r["erasure_id"]), str(r["scope_type"]), str(r["state"]))
        for r in rows
        if _covers(str(r["scope_type"]), str(r["scope_value"]), namespace)
    ]


async def _await_erasures(held: Held, since_s: int, ch: _CHClient) -> None:
    """Wait until every erasure covering the original that had not finished at
    `since_s` has completed; `SwitchedUnderErasure` after ERASURE_WAIT_S."""
    deadline = time.monotonic() + ERASURE_WAIT_S
    while True:
        raced = await _erasures_since(held.org, held.namespace, since_s, ch)
        open_ = [e for e in raced if not e.done]
        if not open_:
            return
        # A failed erasure finishes only through a re-verify: waiting gains nothing.
        if any(e.status == "failed" for e in open_):
            raise SwitchedUnderErasure(
                _fence_advice(
                    open_, "started as its row was switched; the switched row is left to it"
                )
            )
        if time.monotonic() >= deadline:
            raise SwitchedUnderErasure(
                f"{open_[0]} started as its row was switched and has not finished within "
                f"{ERASURE_WAIT_S} s; the switched row and its object are left to it"
            )
        await asyncio.sleep(_ERASURE_POLL_S)


@dataclass(frozen=True)
class Fence:
    """A namespace's erasure fence as one read of the ledger sees it: when (on the
    ClickHouse clock, in seconds) and what refusal holds every original of it, if any.
    Coverage depends on the namespace alone, so one read serves a page of the walk."""

    since: int
    refusal: str = ""


async def fence(org: str, namespace: str, *, ch: _CHClient | None = None) -> Fence:
    """The namespace's fence now: the refusal names the erasure that holds it longest."""
    ch = ch or _default_client()
    since = await _cutoff_ms(0, ch) // 1000
    fenced = await _erasures_since(org, namespace, since, ch)
    return Fence(since, _fence_advice(fenced, "covers it") if fenced else "")


async def move_to_bucket(
    held: Held, fenced: Fence | None = None, *, ch: _CHClient | None = None
) -> bool:
    """Copy one original from its ClickHouse row to a new object in the bucket and
    switch its catalog row to that object; every document naming the sha reads from
    the bucket from then on. False when there is nothing to move: the row went (an
    erasure, the sweep) or was replaced since the walk read it, or an erasure deleted
    the original while it moved.

    The bytes are checked against the size and sha256 before the write, by the store
    as it writes them, and again as read back from the bucket. The object is written
    before the row naming it, as an upload's is, and the row is written only within
    DOCUMENT_UPLOAD_BUDGET_S of the write: past `document_blob_min_age_seconds` the
    bucket walk may take an object no row names. An object no row will name (a row gone
    or replaced meanwhile, an erasure that overtook the copy, a write past the budget)
    is deleted at once; one a crash leaves is the bucket walk's once it is old enough.
    The new row is a later version of the same key: the ClickHouse row's bytes stay
    until the table merges the two. The upload records are keyed by the sha and stay
    as they are.

    A move copies bytes an erasure may be deleting, so it is fenced by the erasure
    ledger, read with sequential consistency. An erasure covers the original when it
    is its namespace's or any user's of the org (`_covers`).

    - The move does not start while the ledger holds a covering erasure that is
      unfinished or finished within `_CLOCK_MARGIN_S` (`ErasureOpen`).
    - One registered while the object was written and read back ends the move before
      its row: it deletes its own object, and an original that erasure kept (a user's
      erasure keeps one another writer still needs) stays held and is reported
      (`ErasureOpen`).
    - One registered between that check and the row landing, the time of one insert,
      may have deleted the original's rows first. A namespace's erasure deletes every
      original of its namespace, so the move deletes its row and object at once. A
      user's erasure deletes every object under an original's prefix when it takes the
      original, the move's included, so the move waits for it to finish and deletes its
      row when its object is gone; when the object is still there the erasure kept the
      original and the switch stands. One that does not finish in ERASURE_WAIT_S, or a
      check that fails, leaves the switched row to it (`SwitchedUnderErasure`).
    - An erasure registered after the row landed reads that row.
    """
    ch = ch or _default_client()
    fenced = fenced or await fence(held.org, held.namespace, ch=ch)
    if fenced.refusal:
        raise ErasureOpen(fenced.refusal)
    since = fenced.since
    data = await read(held.org, held.namespace, held.sha, stored=Stored(held.size, ""), ch=ch)
    if data is None:
        with sequential_reads():
            now = await locate(held.org, held.namespace, held.sha, ch=ch)
        if now is not None and not now.location:
            raise CorruptOriginal("bytes in ClickHouse do not fill its size")
        return False
    if hashlib.sha256(data).hexdigest() != held.sha:
        raise CorruptOriginal("bytes in ClickHouse do not hash to its sha256")
    loc = objects.new_location(held.org, held.namespace, held.sha)
    async with objects.transfer() as lease:
        started = time.monotonic()  # as an upload's: from the write, not the slot's wait
        await objects.put(loc, data, held.sha, lease)
    back = await objects.read(loc)
    if back is not None and hashlib.sha256(back).hexdigest() != held.sha:
        await objects.delete(loc.bucket, [loc.key])
        raise CorruptOriginal("bytes read back from the bucket do not hash to its sha256")
    # The latest row read again just before the switch: an erasure or a re-upload
    # since the walk keeps what it left.
    with sequential_reads():
        now = await locate(held.org, held.namespace, held.sha, ch=ch)
    if now is None or now.location:
        await objects.delete(loc.bucket, [loc.key])  # no row names it, nor will
        return False
    overtaken = await _erasures_since(held.org, held.namespace, since, ch)
    if overtaken or back is None:
        # No row names the object yet. The original is the erasure's: gone, or kept,
        # and then still held here.
        await objects.delete(loc.bucket, [loc.key])
        with sequential_reads():
            after = await locate(held.org, held.namespace, held.sha, ch=ch)
        if after is None or after.location:
            return False
        if overtaken:
            # Still held here: the erasure has not reached it yet, or kept it (a user's
            # erasure keeps one another writer still needs). Reported, not counted as gone.
            raise ErasureOpen(
                _fence_advice(overtaken, "started while it was copied, and it stays in ClickHouse")
            )
        raise objects.ObjectStoreError(
            "object store read failed (its new object was gone before its read-back)",
            operation="read",
        )
    if time.monotonic() - started > DOCUMENT_UPLOAD_BUDGET_S:
        await objects.delete(loc.bucket, [loc.key])
        raise objects.ObjectStoreError(
            f"object store write took longer than {DOCUMENT_UPLOAD_BUDGET_S} s before its row",
            operation="write",
        )
    await ch.insert(
        TABLE,
        [
            {
                "org": held.org,
                "namespace": held.namespace,
                "sha256": held.sha,
                "size": held.size,
                "data": b"",
                "location": loc.uri(),
            }
        ],
        settings={"async_insert": 0},
    )
    return await _settle(held, since, loc, ch)


async def _settle(held: Held, since: int, loc: objects.Location, ch: _CHClient) -> bool:
    """After the switched row landed: an erasure registered as it landed decides. No race:
    the switch stands. A namespace's erasure: the move is taken back. A user's: once it
    has finished, the move is taken back if the erasure took the moved object. A read that
    fails here leaves the switched row, reported (`SwitchedUnderErasure`)."""
    try:
        raced = await _erasures_since(held.org, held.namespace, since, ch)
        namespace_raced = any(e.scope_type != "user_id" for e in raced)
        taken = False
        if raced and not namespace_raced:
            await _await_erasures(held, since, ch)
            taken = await objects.read(loc) is None
    except SwitchedUnderErasure:
        raise
    except Exception as e:  # the row is switched: an unread ledger or object is reported, not lost
        raise SwitchedUnderErasure(
            f"switched to {loc.uri()}, but the check for an erasure that raced it failed ({e}); "
            "check that no erasure of its namespace or a user ran then"
        ) from e
    if not (namespace_raced or taken):
        # An original nothing needs any more by now (its last document went as it moved)
        # is left to the sweep, which deletes it once older than its min age: a delete
        # here, unbounded by age, could take the only row a racing writer's merge left.
        return True
    try:
        await _unwrite(held, loc, ch)
    except Exception as e:  # the row is switched: a failed take-back is reported, not lost
        raise SwitchedUnderErasure(
            f"switched to {loc.uri()} as an erasure took it, but taking the switch back failed "
            f"({e}); run that erasure again"
        ) from e
    return False


async def _unwrite(held: Held, loc: objects.Location, ch: _CHClient) -> None:
    """Delete the row a move wrote and its object: the row by the location only it
    names, then the object."""
    await ch.execute(
        f"DELETE FROM {TABLE} WHERE {_KEY} AND location = {{loc:String}}",
        params={"org": held.org, "ns": held.namespace, "sha": held.sha, "loc": loc.uri()},
    )
    try:
        await objects.delete(loc.bucket, [loc.key])
    except objects.ObjectStoreError as e:
        raise objects.ObjectStoreError(
            f"the switched row was taken back, but deleting its object failed ({e}); the bucket "
            "walk takes it",
            operation="delete",
        ) from e


@dataclass(frozen=True)
class Deleted:
    gone: int  # originals whose rows are gone
    kept: int = 0  # objects outside the configured bucket, or any with it unset: rows kept
    error: objects.ObjectStoreError | None = None  # the bucket refused the objects' delete


async def delete(
    org: str,
    namespace: str,
    shas: list[str],
    *,
    min_age_s: int = 0,
    ch: _CHClient | None = None,
) -> Deleted:
    """Delete the originals `shas` of the namespace: the rows in one statement,
    then the objects no row names any more, then the record of who uploaded
    the ones now gone. With `min_age_s`, only rows written at least that long
    ago (on the ClickHouse clock), so an original a concurrent write just
    stored again survives.

    Each write has its own key, so an object no row names after the delete can
    never be named again (a concurrent write of the same bytes names a new key).
    A row naming an object outside the configured bucket, or any object with no
    bucket configured, is kept: deleting it would leave bytes nothing finds.
    When the bucket refuses the objects' delete, the upload records stay, so a
    user erasure still finds the bytes, and the bucket walk retries them."""
    shas = sorted(set(shas))
    if not shas:
        return Deleted(0)
    ch = ch or _default_client()
    scope = "org = {org:String} AND namespace = {ns:String} AND sha256 IN {shas:Array(String)}"
    where = scope
    params: dict[str, object] = {"org": org, "ns": namespace, "shas": shas}
    if min_age_s > 0:
        where += f" AND {_OLDER_THAN_CUTOFF}"
        params["cutoff_ms"] = await _cutoff_ms(min_age_s, ch)
    # Candidates under the delete's own age bound: a fresh write's object is never
    # one, so a replica that lags behind the delete can only make this keep an object.
    bucket = settings.document_s3_bucket
    candidates = await _locations(where, params, ch)
    unreachable = {u for u in candidates if objects.Location.parse(u).bucket != bucket}
    # The rows it may delete, named by what they are, not by what a read found:
    # a row of the ClickHouse store, or one naming an object in the configured
    # bucket. A replica behind can then only keep a row, never take another's.
    reachable = "location = ''"
    if bucket:
        reachable += " OR startsWith(location, {mine:String})"
        params = {**params, "mine": f"{objects.SCHEME}{bucket}/"}
    await ch.execute(f"DELETE FROM {TABLE} WHERE {where} AND ({reachable})", params=params)
    rows = await ch.query_all(
        f"SELECT DISTINCT toString(sha256) AS sha, location FROM {TABLE} WHERE {scope}",
        params={"org": org, "ns": namespace, "shas": shas},
    )
    gone = sorted(set(shas) - {str(r["sha"]) for r in rows})
    unnamed = candidates - unreachable - {str(r["location"]) for r in rows}
    try:
        await objects.delete(bucket, [objects.Location.parse(u).key for u in sorted(unnamed)])
    except objects.ObjectStoreError as e:
        return Deleted(len(gone), len(unreachable), e)
    if gone:
        # Who uploaded them goes with them; a record younger than `min_age_s`
        # may be a re-upload's, written just before its bytes.
        await ch.execute(f"DELETE FROM {UPLOADS} WHERE {where}", params={**params, "shas": gone})
    return Deleted(len(gone), len(unreachable))


async def forget_uploads(
    keys: list[tuple[str, str, str]], min_age_s: int, *, ch: _CHClient | None = None
) -> None:
    """Delete the upload records of the originals `keys` names, their (org,
    namespace, sha256), once no catalog row holds them and the bucket walk
    deleted their last object: a write whose row never landed, or a sweep that
    kept the records because the bucket refused its delete. A record younger
    than `min_age_s` may be a write in flight. The keys go as many to a statement
    as one parameter carries (`ch_params.chunks`)."""
    if not keys:
        return
    ch = ch or _default_client()
    cutoff_ms = await _cutoff_ms(min_age_s, ch)
    # The keys a row holds are read, then left out by value: a replicated table refuses
    # a subquery in a mutation. An end user's write landing after the read brings an
    # upload record younger than the cutoff, which the delete keeps.
    with sequential_reads():
        _, held = await _named(keys, ch)
    for part in chunks([k for k in keys if k not in held]):
        await ch.execute(
            f"DELETE FROM {UPLOADS} WHERE {_IN_KEYS} AND {_OLDER_THAN_CUTOFF}",
            params={"keys": part, "cutoff_ms": cutoff_ms},
        )


async def _locations(where: str, params: dict[str, object], ch: _CHClient) -> set[str]:
    rows = await ch.query_all(
        f"SELECT DISTINCT location FROM {TABLE} WHERE {where} AND location != ''", params=params
    )
    return {str(r["location"]) for r in rows}


async def namespaces(
    after: tuple[str, str], limit: int, *, org: str = "", ch: _CHClient | None = None
) -> list[tuple[str, str]]:
    """Up to `limit` (org, namespace) pairs holding originals, in key order after
    `after`, of `org` alone when named: the sweep and the move to the bucket walk
    them one namespace at a time."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT DISTINCT org, namespace FROM {TABLE} "
        "WHERE (org, namespace) > ({org:String}, {ns:String}) "
        + ("AND org = {only:String} " if org else "")
        + "ORDER BY org, namespace LIMIT {lim:UInt32}",
        params={"org": after[0], "ns": after[1], "only": org, "lim": int(limit)},
    )
    return [(str(r["org"]), str(r["namespace"])) for r in rows]


async def unreferenced(
    org: str,
    namespace: str,
    min_age_s: int,
    limit: int,
    *,
    after: str = "",
    ch: _CHClient | None = None,
) -> list[str]:
    """Up to `limit` shas of the namespace's originals, in order after `after`,
    that no live document names (as its original or as its extraction
    artifact), no unexpired upload or open ingestion needs, and nothing has
    written for `min_age_s`: those of deleted and re-uploaded documents, of
    documents retention removed, and of uploads never ingested."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT toString(sha256) AS sha
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND sha256 > {{after:String}}
          AND toString(sha256) NOT IN (
              SELECT arrayJoin([blob_path, artifact_sha256]) FROM loom.documents FINAL
              WHERE org = {{org:String}} AND namespace = {{ns:String}} AND {LIVE_ORIGINAL_SQL}
          )
          AND toString(sha256) NOT IN ({HELD_SQL})
        GROUP BY sha256
        HAVING max(created_at) < now64(3) - INTERVAL {{age:UInt32}} SECOND
        ORDER BY sha256
        LIMIT {{lim:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "after": after,
            "age": int(min_age_s),
            "lim": int(limit),
        },
    )
    return [str(r["sha"]) for r in rows]


async def _named(
    keys: list[tuple[str, str, str]], ch: _CHClient
) -> tuple[set[str], set[tuple[str, str, str]]]:
    """The locations every row of `keys` (their org, namespace and sha256, the
    table's sort key) names, replaced rows not yet merged included, so no object
    a row can still be read from counts as unnamed; and the keys some row holds."""
    rows: list[dict[str, Any]] = []
    for part in chunks(keys):
        rows += await ch.query_all(
            f"SELECT DISTINCT location, org, namespace, toString(sha256) AS sha FROM {TABLE} "
            f"WHERE {_IN_KEYS}",
            params={"keys": part},
        )
    held = {(str(r["org"]), str(r["namespace"]), str(r["sha"])) for r in rows}
    return {str(r["location"]) for r in rows}, held


@dataclass(frozen=True)
class Unnamed:
    keys: list[str]  # objects no row names, old enough to delete
    # Among them, the (org, namespace, sha256) no row holds at all: their upload
    # records may go once their prefix lists empty (rare: a write whose row never
    # landed, or a delete the store refused).
    orphans: list[tuple[str, str, str]]
    after: str  # the key the next page starts after; '' at the end of the bucket


async def unnamed_objects(
    start_after: str, limit: int, min_age_s: int, *, ch: _CHClient | None = None
) -> Unnamed:
    """One page of the bucket's originals, the `limit` keys after `start_after`:
    the keys of objects that no row names and that are at least `min_age_s` old
    by the store's own clock (an object is written before its row), and the
    originals among them no row holds at all. A key that is not an original's
    is left out."""
    ch = ch or _default_client()
    bucket = settings.document_s3_bucket
    page = await objects.list_page(
        bucket, objects.root_prefix(), start_after=start_after, max_keys=limit
    )
    # The clock that stamped them. `Date` and `LastModified` are both cut to whole
    # seconds, so an age is right to about a second either way, far inside the
    # min-age floor's margin.
    cutoff = page.at - timedelta(seconds=min_age_s)
    scoped = [(o, objects.scope_of(o.key)) for o in page.objects]
    old = [(o.key, sk) for o, sk in scoped if sk is not None and o.modified < cutoff]
    stale: list[str] = []
    orphans: set[tuple[str, str, str]] = set()
    if old:
        # Sequential: a replica behind a row just written would call its object
        # unnamed, and this read alone decides a delete the age bound does not cover.
        with sequential_reads():
            named, held = await _named(sorted({sk for _, sk in old}), ch)
        for k, sk in old:
            if objects.Location(bucket, k).uri() not in named:
                stale.append(k)
                if sk not in held:
                    orphans.add(sk)
    after = page.objects[-1].key if page.more and page.objects else ""
    return Unnamed(stale, sorted(orphans), after)
