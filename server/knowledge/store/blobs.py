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


async def ch_now_s(*, ch: _CHClient | None = None) -> int:
    """Now on the ClickHouse clock, in whole seconds: what the erasure ledger's times are
    read against."""
    return await _cutoff_ms(0, ch or _default_client()) // 1000


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


async def switch_to(held: Held, loc: objects.Location, *, ch: _CHClient | None = None) -> None:
    """Write the catalog row that switches the original to the object at `loc`: a later
    version of its key, with no bytes."""
    ch = ch or _default_client()
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


async def row_names(held: Held, loc: objects.Location, *, ch: _CHClient | None = None) -> bool:
    """Whether a row of the original still names the object at `loc`."""
    ch = ch or _default_client()
    with sequential_reads():
        row = await ch.query_one(
            f"SELECT count() AS n FROM {TABLE} WHERE {_KEY} AND location = {{loc:String}}",
            params={"org": held.org, "ns": held.namespace, "sha": held.sha, "loc": loc.uri()},
        )
    return bool(row and int(row["n"]))


async def unwrite(held: Held, loc: objects.Location, *, ch: _CHClient | None = None) -> None:
    """Delete the switch to `loc`: the row by the location only it names, then the
    object, which the bucket walk takes when its delete fails."""
    ch = ch or _default_client()
    await ch.execute(
        f"DELETE FROM {TABLE} WHERE {_KEY} AND location = {{loc:String}}",
        params={"org": held.org, "ns": held.namespace, "sha": held.sha, "loc": loc.uri()},
    )
    try:
        await objects.delete(loc.bucket, [loc.key])
    except objects.ObjectStoreError as e:
        log.warning("blobs: %s taken back; its object is left to the bucket walk: %s", loc.uri(), e)


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
