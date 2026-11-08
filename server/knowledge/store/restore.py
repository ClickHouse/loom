"""The statements `knowledge.restore` runs: which ingestions a delete retired, whether an
erasure reached the document, and bringing the ingestions' chunks back.

A delete lists the document's live ingestion in `loom.chunk_supersessions` with the
same `superseded_at` it stamps on the document row, so that time and the revision the
chunks record name the rows the delete wrote. An erasure that keeps rows stamps
`doc_superseded_at` on the chunks and `superseded_at` on the document in place; the
handler reads its request from the erasure ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL, SUPERSESSIONS_TABLE

_DOC = "org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String}"
# A chunk of the revision, or one written before chunks recorded their revision, which
# only the time of the delete that listed it tells apart.
_OF_REVISION = (
    "(JSONExtractUInt(chunk_metadata, 'revision') = {rev:UInt64}"
    " OR NOT JSONHas(chunk_metadata, 'revision'))"
)


async def retired_by_delete(
    org: str,
    namespace: str,
    source_uri: str,
    revision: int,
    at: datetime,
    *,
    ch: _CHClient | None = None,
) -> list[int]:
    """The ingestions of the document's `revision` listed as retired at `at`, the time a
    delete stamps on the document row and on these rows; empty when none was."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT doc_version AS v FROM {SUPERSESSIONS_TABLE}
        WHERE {_DOC} AND superseded_at = {{at:DateTime}}
          AND doc_version IN (
              SELECT DISTINCT doc_version FROM loom.chunks
              WHERE {_DOC} AND {_OF_REVISION}
          )
        ORDER BY v
        """,
        params={"org": org, "ns": namespace, "uri": source_uri, "at": at, "rev": revision},
    )
    return [int(r["v"]) for r in rows]


async def live_versions(
    org: str, namespace: str, source_uri: str, revision: int, *, ch: _CHClient | None = None
) -> list[int]:
    """The ingestions of the document's `revision` whose chunks read as live: what a
    restore that removed the delete's rows and then failed before the row leaves."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT DISTINCT doc_version AS v FROM loom.chunks WHERE {_DOC}"
        f" AND {_OF_REVISION} AND {LIVE_CHUNK_SQL} ORDER BY v",
        params={"org": org, "ns": namespace, "uri": source_uri, "rev": revision},
    )
    return [int(r["v"]) for r in rows]


async def erased_chunks(
    org: str,
    namespace: str,
    source_uri: str,
    versions: Sequence[int],
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Whether an erasure stamped any chunk of these ingestions."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT count() AS n FROM loom.chunks WHERE {_DOC}"
        " AND doc_version IN {vs:Array(UInt64)} AND doc_superseded_at IS NOT NULL",
        params={"org": org, "ns": namespace, "uri": source_uri, "vs": list(versions)},
    )
    return bool(row and int(row["n"]))


async def unretire(
    org: str,
    namespace: str,
    source_uri: str,
    versions: Sequence[int],
    at: datetime,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Remove the rows the delete wrote for these ingestions, under every embedding model,
    so their chunks read as live again. A row an earlier retirement wrote (another time)
    stays."""
    ch = ch or _default_client()
    await ch.execute(
        f"DELETE FROM {SUPERSESSIONS_TABLE} WHERE {_DOC}"
        " AND doc_version IN {vs:Array(UInt64)} AND superseded_at = {at:DateTime}",
        params={"org": org, "ns": namespace, "uri": source_uri, "vs": list(versions), "at": at},
    )


async def delete_row(
    org: str, namespace: str, source_uri: str, version: int, *, ch: _CHClient | None = None
) -> None:
    """Remove the one document row a restore wrote (its own version), when it takes itself
    back under a purge that may cover the document."""
    ch = ch or _default_client()
    await ch.execute(
        f"DELETE FROM loom.documents WHERE {_DOC} AND version = {{v:UInt64}}",
        params={"org": org, "ns": namespace, "uri": source_uri, "v": version},
    )
