"""loom.document_blobs: the uploaded originals behind `loom.documents.blob_path`.

A document's `blob_path` is the sha256 of its original's bytes; the bytes live in
one row per (org, namespace, sha256), so re-uploading the same file writes the
same row and two documents with the same bytes share it.

`created_at` is the row's version, so a re-upload restamps it. Outside erasure
only the sweep (`server/knowledge/workers/blob_gc.py`) deletes an original, and
only one unwritten for `document_blob_min_age_seconds`: a write stores its
original before the document row that names it, so a check that finds no
document naming a fresh original may be racing that write.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.knowledge.store.documents import LIVE_ORIGINAL_SQL

TABLE = "loom.document_blobs"
UPLOADS = "loom.blob_uploads"
_KEY = "org = {org:String} AND namespace = {ns:String} AND sha256 = {sha:String}"


async def put(
    org: str, namespace: str, data: bytes, *, uploader: str = "-", ch: _CHClient | None = None
) -> str:
    """Store `data` and return its sha256, the document's `blob_path`. Committed
    before it returns: the document row that names it is written next. An end
    user's upload is recorded first (`loom.blob_uploads`), so their erasure
    finds every original they stored."""
    sha = hashlib.sha256(data).hexdigest()
    ch = ch or _default_client()
    if uploader not in ("", "-"):
        await ch.insert(
            UPLOADS,
            [{"org": org, "namespace": namespace, "sha256": sha, "user_id": uploader}],
            settings={"async_insert": 0},
        )
    await ch.insert(
        TABLE,
        [{"org": org, "namespace": namespace, "sha256": sha, "data": data, "size": len(data)}],
        settings={"async_insert": 0},
    )
    return sha


async def size(org: str, namespace: str, sha: str, *, ch: _CHClient | None = None) -> int | None:
    """The original's byte count, or None when no row holds it. Reads no data."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT size FROM {TABLE} WHERE {_KEY} LIMIT 1",
        params={"org": org, "ns": namespace, "sha": sha},
    )
    return int(row["size"]) if row else None


async def read(org: str, namespace: str, sha: str, *, ch: Any = None) -> bytes:
    """The original's bytes, buffered; empty when no row holds it."""
    from server.core.ch import get_client

    ch = ch or get_client()
    data: bytes = await ch.raw_bytes(
        f"SELECT data FROM {TABLE} WHERE {_KEY} LIMIT 1 FORMAT RawBLOB",
        {"org": org, "ns": namespace, "sha": sha},
    )
    return data


@asynccontextmanager
async def stream(
    org: str, namespace: str, sha: str, *, ch: Any = None
) -> AsyncIterator[AsyncIterator[bytes]]:
    """The original's bytes as chunks, straight off the HTTP response; empty
    when no row holds it."""
    from server.core.ch import get_client

    ch = ch or get_client()
    async with ch.raw_stream(
        f"SELECT data FROM {TABLE} WHERE {_KEY} LIMIT 1 FORMAT RawBLOB",
        {"org": org, "ns": namespace, "sha": sha},
    ) as chunks:
        yield chunks


async def delete(
    org: str,
    namespace: str,
    shas: list[str],
    *,
    min_age_s: int = 0,
    ch: _CHClient | None = None,
) -> int:
    """Delete the originals `shas` of the namespace in one statement, and the
    record of who uploaded the ones now gone, and return how many are gone; with
    `min_age_s`, only rows written at least that long ago (on the ClickHouse
    clock), so an original a concurrent write just stored again survives."""
    shas = sorted(set(shas))
    if not shas:
        return 0
    ch = ch or _default_client()
    where = "org = {org:String} AND namespace = {ns:String} AND sha256 IN {shas:Array(String)}"
    params: dict[str, object] = {"org": org, "ns": namespace, "shas": shas}
    if min_age_s > 0:
        # The cutoff is read first and bound: a replicated table refuses now64() in a
        # mutation.
        row = await ch.query_one(
            "SELECT toUnixTimestamp64Milli(now64(3)) - {age:UInt32} * 1000 AS cutoff_ms",
            params={"age": int(min_age_s)},
        )
        if row is None:
            raise RuntimeError("SELECT without FROM returned no row")
        where += " AND created_at < fromUnixTimestamp64Milli({cutoff_ms:Int64})"
        params["cutoff_ms"] = int(row["cutoff_ms"])
    await ch.execute(f"DELETE FROM {TABLE} WHERE {where}", params=params)
    rows = await ch.query_all(
        f"SELECT DISTINCT toString(sha256) AS sha FROM {TABLE} "
        "WHERE org = {org:String} AND namespace = {ns:String} AND sha256 IN {shas:Array(String)}",
        params={"org": org, "ns": namespace, "shas": shas},
    )
    gone = sorted(set(shas) - {str(r["sha"]) for r in rows})
    if gone:
        # Who uploaded them goes with them; a record younger than `min_age_s`
        # may be a re-upload's, written just before its bytes.
        await ch.execute(f"DELETE FROM {UPLOADS} WHERE {where}", params={**params, "shas": gone})
    return len(gone)


async def namespaces(
    after: tuple[str, str], limit: int, *, ch: _CHClient | None = None
) -> list[tuple[str, str]]:
    """Up to `limit` (org, namespace) pairs holding originals, in key order after
    `after`: the sweep walks them one namespace at a time."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT DISTINCT org, namespace FROM {TABLE} "
        "WHERE (org, namespace) > ({org:String}, {ns:String}) "
        "ORDER BY org, namespace LIMIT {lim:UInt32}",
        params={"org": after[0], "ns": after[1], "lim": int(limit)},
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
    that no live document names and nothing has written for `min_age_s`: those
    of deleted and re-uploaded documents, and of documents retention removed."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT toString(sha256) AS sha
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND sha256 > {{after:String}}
          AND toString(sha256) NOT IN (
              SELECT blob_path FROM loom.documents FINAL
              WHERE org = {{org:String}} AND namespace = {{ns:String}} AND {LIVE_ORIGINAL_SQL}
          )
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
