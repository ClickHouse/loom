"""Repository for the loom.documents table (chunk-level rows live in knowledge_repo).

ReplacingMergeTree(version) keyed on (org, namespace, source_uri); FINAL
collapses to the latest version of each doc.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from pydantic import BaseModel, ConfigDict

from server.core.hashing import hash_text, sha256_document_text
from server.core.repo_base import _CHClient, _default_client
from server.core.rmt_carry import register
from server.core.tag_sql import TagFilter, tag_where
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL


class DocumentRow(BaseModel):
    """Read shape. `metadata` stays the JSON-encoded string the column stores."""

    model_config = ConfigDict(frozen=True)

    org: str
    namespace: str
    source_uri: str
    doc_id: str
    title: str = ""
    description: str = ""
    content: str
    tags: list[str] = []
    metadata: str = "{}"
    version: int
    superseded_at: datetime | None = None
    ingested_at: datetime
    content_sha256: str = ""
    blob_path: str = ""
    blob_size: int = 0
    blob_content_type: str = ""
    original_filename: str = ""
    blob_erased_at: datetime | None = None
    # Who wrote it and what it is (provenance, erasure by user); access never reads these.
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"
    actor: str = ""
    source_class: str = ""
    # The source's revision (1, 2, ... per source_uri; 0 on a row no write numbered)
    # and, for a file Loom parsed, its format, extraction artifact (a sha256 in
    # loom.document_blobs), parser, coverage JSON and the ingestion that published it.
    revision: int = 0
    format: str = ""
    artifact_sha256: str = ""
    parser: str = ""
    coverage: str = ""
    ingestion_id: str = ""


def _hydrate(row: dict[str, Any]) -> DocumentRow:
    return DocumentRow(
        org=row["org"],
        namespace=row["namespace"],
        source_uri=row["source_uri"],
        doc_id=str(row["doc_id"]),
        title=row.get("title", "") or "",
        description=row.get("description", "") or "",
        content=row["content"],
        tags=list(row.get("tags") or []),
        metadata=row.get("metadata") or "{}",
        version=int(row["version"]),
        superseded_at=row.get("superseded_at"),
        ingested_at=row["ingested_at"],
        content_sha256=hash_text(row.get("content_sha256")),
        blob_path=row.get("blob_path") or "",
        blob_size=int(row.get("blob_size") or 0),
        blob_content_type=row.get("blob_content_type") or "",
        original_filename=row.get("original_filename") or "",
        blob_erased_at=row.get("blob_erased_at"),
        agent=row.get("agent") or "-",
        user_id=row.get("user_id") or "-",
        session_id=row.get("session_id") or "-",
        actor=row.get("actor") or "",
        source_class=row.get("source_class") or "",
        revision=int(row.get("revision") or 0),
        format=row.get("format") or "",
        artifact_sha256=row.get("artifact_sha256") or "",
        parser=row.get("parser") or "",
        coverage=row.get("coverage") or "",
        ingestion_id=row.get("ingestion_id") or "",
    )


async def get_one(
    org: str,
    namespace: str,
    source_uri: str,
    *,
    include_superseded: bool = False,
    ch: _CHClient | None = None,
) -> DocumentRow | None:
    """Fetch one document by (org, namespace, source_uri).

    include_superseded=False returns only the live row; True returns the
    latest row regardless of supersede state (audit/replay).
    """
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "source_uri = {uri:String}",
    ]
    if not include_superseded:
        where.append("superseded_at IS NULL")
    row = await ch.query_one(
        f"""
        SELECT org, namespace, source_uri, doc_id, title, description, content,
               tags, metadata, version, superseded_at, ingested_at, blob_path, blob_size,
               toString(blob_content_type) AS blob_content_type,
               original_filename, content_sha256, blob_erased_at,
               agent, user_id, session_id, actor, toString(source_class) AS source_class,
               revision, toString(format) AS format, artifact_sha256, parser, coverage,
               ingestion_id
        FROM loom.documents FINAL
        WHERE {" AND ".join(where)}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "uri": source_uri},
    )
    return _hydrate(row) if row else None


async def revision_by_ingestion(
    org: str, namespace: str, source_uri: str, ingestion_id: str, *, ch: _CHClient | None = None
) -> int | None:
    """The revision an ingestion job published for the source, from any version
    row of it that names the job, the latest or not: a later write of the
    source (a text write, which names no job) does not hide it. Read without
    FINAL; None when no row names the job (it did not publish, or merges have
    folded its row into a later write's)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT revision FROM loom.documents"
        " WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String}"
        " AND ingestion_id = {id:String} ORDER BY version DESC LIMIT 1",
        params={"org": org, "ns": namespace, "uri": source_uri, "id": ingestion_id},
    )
    return int(row["revision"]) if row else None


# A document that still serves its original: a write without fresh bytes carries
# such a row's original forward, and the sweep keeps every original one names.
LIVE_ORIGINAL_SQL = "blob_path != '' AND superseded_at IS NULL AND blob_erased_at IS NULL"


def serves_original(row: DocumentRow | None) -> bool:
    """`LIVE_ORIGINAL_SQL` for a row already read."""
    return (
        row is not None
        and bool(row.blob_path)
        and row.superseded_at is None
        and row.blob_erased_at is None
    )


async def current_document_tally(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> tuple[int, datetime | None]:
    """Current documents of the namespace (superseded versions left out), and the
    newest ingest among them (None when there is none)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT count() AS n, if(n = 0, NULL, max(ingested_at)) AS latest "
        "FROM loom.documents FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND superseded_at IS NULL",
        params={"org": org, "ns": namespace},
    )
    return (int(row["n"]), row["latest"]) if row else (0, None)


async def get_many_by_uris(
    org: str,
    namespace: str,
    source_uris: list[str],
    *,
    include_superseded: bool = False,
    ch: _CHClient | None = None,
) -> list[DocumentRow]:
    """Bulk read across N URIs in one round-trip. Empty list → no CH call."""
    if not source_uris:
        return []
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "source_uri IN {uris:Array(String)}",
    ]
    if not include_superseded:
        where.append("superseded_at IS NULL")
    rows = await ch.query_all(
        f"""
        SELECT org, namespace, source_uri, doc_id, title, description, content,
               tags, metadata, version, superseded_at, ingested_at, blob_path, blob_size,
               toString(blob_content_type) AS blob_content_type,
               original_filename, content_sha256, blob_erased_at,
               agent, user_id, session_id, actor, toString(source_class) AS source_class,
               revision, toString(format) AS format, artifact_sha256, parser, coverage,
               ingestion_id
        FROM loom.documents FINAL
        WHERE {" AND ".join(where)}
        """,
        params={"org": org, "ns": namespace, "uris": source_uris},
    )
    return [_hydrate(r) for r in rows]


async def live_versions(
    org: str, namespace: str, source_uris: list[str], *, ch: _CHClient | None = None
) -> dict[str, int]:
    """Each source's live document version, in one read of the key alone; a
    source with no live document is absent."""
    if not source_uris:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT source_uri, version FROM loom.documents FINAL"
        " WHERE org = {org:String} AND namespace = {ns:String}"
        " AND source_uri IN {uris:Array(String)} AND superseded_at IS NULL"
        " LIMIT {n:UInt32}",
        params={"org": org, "ns": namespace, "uris": source_uris, "n": len(source_uris)},
    )
    return {str(r["source_uri"]): int(r["version"]) for r in rows}


class DocumentListRow(TypedDict):
    """One `knowledge.list` row: the document's listing columns and its live
    chunk count."""

    source_uri: str
    title: str
    description: str
    tags: list[str]
    ingested_at: datetime
    superseded_at: datetime | None
    blob_path: str
    blob_size: int
    blob_content_type: str
    original_filename: str
    content_preview: str
    chunk_count: int


async def list_with_chunk_counts(
    org: str,
    namespace: str,
    *,
    include_superseded: bool = False,
    tag_filter: TagFilter | None = None,
    uri_prefixes: list[str] | None = None,
    cursor: tuple[datetime, str] | None = None,
    limit: int = 50,
    ch: _CHClient | None = None,
) -> list[DocumentListRow]:
    """Paginated source_uri listing with per-doc live-chunk counts.

    Keyset cursor on (ingested_at DESC, source_uri DESC) — pass the last
    row's (ingested_at, source_uri) to skip past it.
    """
    ch = ch or _default_client()
    extra_where = ""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "lim": int(limit),
    }
    # Predicates are bare (no `d.` prefix) because they filter the inner
    # FINAL subquery below, not the post-join scope.
    if not include_superseded:
        extra_where += " AND superseded_at IS NULL"
    if tag_filter is not None:
        extra_where += f" AND {tag_where(tag_filter, params)}"
    live_prefixes = [p for p in (uri_prefixes or []) if p]
    if live_prefixes:
        # Directory scope: OR of startsWith over the (org, namespace,
        # source_uri) sort key.
        clauses = []
        for i, p in enumerate(live_prefixes):
            clauses.append(f"startsWith(source_uri, {{pfx{i}:String}})")
            params[f"pfx{i}"] = p
        extra_where += " AND (" + " OR ".join(clauses) + ")"
    if cursor is not None:
        cur_ts, cur_uri = cursor
        if cur_ts.tzinfo is not None:
            from datetime import UTC

            cur_ts = cur_ts.astimezone(UTC).replace(tzinfo=None)
        extra_where += (
            " AND (ingested_at, source_uri) < "
            "(toDateTime({cur_ts:String}, 'UTC'), {cur_uri:String})"
        )
        params["cur_ts"] = cur_ts.strftime("%Y-%m-%d %H:%M:%S")
        params["cur_uri"] = cur_uri
    # The preview is the stored `content_preview`, never a
    # substring of `content`: ClickHouse reads a column to take a substring
    # of it, so one page would read every document's text (12 MB for 37 kB).
    # Filter + FINAL + order + limit the documents in an isolated subquery,
    # THEN LEFT JOIN the per-doc chunk counts. CH 26.5's analyzer mishandles a
    # WHERE on a FINAL table that is also a JOIN side (it tries to evaluate the
    # whole AND predicate as a column against an empty FINAL block →
    # NOT_FOUND_COLUMN_IN_BLOCK). Pre-filtering in a derived table sidesteps
    # that and is cheaper — only the limited rows reach the join.
    rows = await ch.query_all(
        f"""
        SELECT d.source_uri        AS source_uri,
               d.title             AS title,
               d.description       AS description,
               d.tags              AS tags,
               d.ingested_at       AS ingested_at,
               d.superseded_at     AS superseded_at,
               d.blob_path         AS blob_path,
               d.blob_size         AS blob_size,
               toString(d.blob_content_type) AS blob_content_type,
               d.original_filename AS original_filename,
               d.content_preview   AS content_preview,
               ifNull(c.chunk_count, 0) AS chunk_count
        FROM (
            SELECT org, namespace, source_uri, title, description, tags,
                   ingested_at, superseded_at, blob_path, blob_size,
                   blob_content_type, original_filename, content_preview
            FROM loom.documents FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
                  {extra_where}
            ORDER BY ingested_at DESC, source_uri DESC
            LIMIT {{lim:UInt32}}
        ) AS d
        LEFT JOIN (
            -- Scoped to the page's namespace: the join key alone does not
            -- push into this side, which would count every tenant's chunks.
            SELECT org, namespace, source_uri, count() AS chunk_count
            FROM loom.chunks
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND {LIVE_CHUNK_SQL}
            GROUP BY org, namespace, source_uri
        ) AS c
          ON d.org = c.org AND d.namespace = c.namespace
         AND d.source_uri = c.source_uri
        ORDER BY d.ingested_at DESC, d.source_uri DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return cast(list[DocumentListRow], rows)


# Every column a writer carries, in schema.sql order (`content_preview` is
# MATERIALIZED, so no writer sends it). A unit test pins this to schema.sql.
DOCUMENT_COLUMNS: tuple[str, ...] = (
    "org",
    "namespace",
    "source_uri",
    "doc_id",
    "title",
    "content",
    "tags",
    "metadata",
    "version",
    "superseded_at",
    "ingested_at",
    "content_sha256",
    "blob_path",
    "blob_size",
    "blob_content_type",
    "original_filename",
    "description",
    "blob_erased_at",
    "agent",
    "user_id",
    "session_id",
    "actor",
    "source_class",
    "revision",
    "format",
    "artifact_sha256",
    "parser",
    "coverage",
    "ingestion_id",
)

_REQUIRED_DOCUMENT_COLUMNS = ("org", "namespace", "source_uri", "content", "version")

# A version row carries any column of the live table this build does not write
# (server/core/rmt_carry.py): a later build's columns survive this build's
# metadata edits and deletes. The key is the table's ORDER BY.
register("loom.documents", key=("org", "namespace", "source_uri"), columns=DOCUMENT_COLUMNS)


class DocumentInsertRow(TypedDict):
    """A full loom.documents row, every column of DOCUMENT_COLUMNS."""

    org: str
    namespace: str
    source_uri: str
    doc_id: str
    title: str
    content: str
    tags: list[str]
    metadata: str
    version: int
    superseded_at: datetime | None
    ingested_at: datetime
    content_sha256: str
    blob_path: str
    blob_size: int
    blob_content_type: str
    original_filename: str
    description: str
    blob_erased_at: datetime | None
    agent: str
    user_id: str
    session_id: str
    actor: str
    source_class: str
    revision: int
    format: str
    artifact_sha256: str
    parser: str
    coverage: str
    ingestion_id: str


def build_document_row(existing: DocumentRow | None = None, /, **fields: Any) -> DocumentInsertRow:
    """The one way to build a loom.documents row for insert.

    loom.documents is ReplacingMergeTree(version): a version row that omits a
    column resets it for every FINAL reader. The row starts from the schema
    defaults, then takes every column of `existing` (carry-forward), then
    `fields`. `content_sha256` is not settable: it is recomputed from the
    row's final title + content, the hash knowledge.add / upsert dedup on.
    """
    unknown = set(fields) - (set(DOCUMENT_COLUMNS) - {"content_sha256"})
    if unknown:
        raise ValueError(f"build_document_row: not settable: {sorted(unknown)}")
    row: dict[str, Any] = {
        "doc_id": str(uuid.uuid4()),
        "title": "",
        "tags": [],
        "metadata": "{}",
        "superseded_at": None,
        "ingested_at": datetime.now(UTC).replace(tzinfo=None),
        "blob_path": "",
        "blob_size": 0,
        "blob_content_type": "",
        "original_filename": "",
        "description": "",
        "blob_erased_at": None,
        "agent": "-",
        "user_id": "-",
        "session_id": "-",
        "actor": "",
        "source_class": "",
        "revision": 0,
        "format": "",
        "artifact_sha256": "",
        "parser": "",
        "coverage": "",
        "ingestion_id": "",
    }
    if existing is not None:
        row.update({c: getattr(existing, c) for c in DOCUMENT_COLUMNS if c != "content_sha256"})
    row.update(fields)
    missing = [c for c in _REQUIRED_DOCUMENT_COLUMNS if c not in row]
    if missing:
        raise ValueError(f"build_document_row: missing {missing}")
    row["content_sha256"] = sha256_document_text(f"{row['title']}\n{row['content']}")
    return cast(DocumentInsertRow, {c: row[c] for c in DOCUMENT_COLUMNS})


async def insert_one(
    row: DocumentInsertRow,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert one document row.

    async_insert=0 so the row is visible to the next FINAL read — callers
    depend on the search-visibility contract.
    """
    ch = ch or _default_client()
    await ch.insert(
        "loom.documents",
        [cast(dict[str, Any], row)],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def insert_many(
    rows: list[DocumentInsertRow],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Bulk insert — returns the number written. Empty → 0, no CH call."""
    if not rows:
        return 0
    ch = ch or _default_client()
    await ch.insert(
        "loom.documents",
        cast(list[dict[str, Any]], rows),
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    return len(rows)


async def agent_opens(
    org: str,
    namespace: str,
    *,
    days: int,
    skip_consumers: Sequence[str],
    source_uris: list[str],
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Per document, the successful `knowledge.get` calls in the last `days`
    (the `agent_events` rows whose `args['source_uri']` names it): count,
    distinct agents, last read. Rows from `skip_consumers` and bench traffic are
    left out; `source_uris` (when not empty) narrows to those documents. A
    primary-key range on (org, namespace) and the window."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT args['source_uri'] AS source_uri, count() AS reads,
               uniqExact(agent) AS agents, max(ts) AS last_read_at
        FROM loom.agent_events
        WHERE org = {org:String} AND namespace = {ns:String}
          AND ts >= now64(3) - toIntervalDay({days:UInt8})
          AND operation = 'knowledge.get' AND success
          AND args['source_uri'] != ''
          AND consumer NOT IN {skip:Array(String)} AND traffic_class != 'bench'
          AND (empty({uris:Array(String)}) OR has({uris:Array(String)}, args['source_uri']))
        GROUP BY source_uri
        ORDER BY reads DESC
        LIMIT 200
        """,
        params={
            "org": org,
            "ns": namespace,
            "days": days,
            "skip": list(skip_consumers),
            "uris": source_uris,
        },
    )
