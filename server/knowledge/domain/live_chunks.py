"""Which rows of loom.chunks are live: the predicate every chunk reader shares."""

from __future__ import annotations

SUPERSESSIONS_TABLE = "loom.chunk_supersessions"


def live_chunk_sql(alias: str = "") -> str:
    """The predicate every reader of loom.chunks uses for "not superseded".

    A chunk is live when `doc_superseded_at IS NULL` and its ingestion (model,
    uri, doc_version) is not listed in
    loom.chunk_supersessions. A plain WHERE term, so the codes-first vector
    scan still applies; the side table holds one row per retired ingestion."""
    p = f"{alias}." if alias else ""
    return (
        f"{p}doc_superseded_at IS NULL"
        f" AND ({p}org, {p}namespace, {p}embedding_model, {p}source_uri, {p}doc_version)"
        f" NOT IN (SELECT org, namespace, embedding_model, source_uri, doc_version"
        f" FROM {SUPERSESSIONS_TABLE})"
    )


LIVE_CHUNK_SQL = live_chunk_sql()


def has_live_chunks_sql(chunks_where: str) -> str:
    """Whether a `loom.documents` row has live chunks of its version or of its revision
    (`chunks_where` narrows the chunks read), so a row-only rewrite (a new list, a
    metadata edit) keeps its chunks. Unlike `OF_REVISION_SQL`, a chunk written before
    chunks recorded their revision counts for its version alone: a copy the sync cannot
    tie to its revision is fetched again. One set of (address, kind, value) serves both
    tests, so ClickHouse reads the chunks once."""
    live = (
        "SELECT source_uri, arrayJoin(if(JSONHas(chunk_metadata, 'revision'), "
        "[('version', doc_version), ('revision', JSONExtractUInt(chunk_metadata, 'revision'))], "
        f"[('version', doc_version)])) FROM loom.chunks WHERE {chunks_where} AND {LIVE_CHUNK_SQL}"
    )
    return (
        f"((source_uri, ('version', version)) IN ({live}) "
        f"OR (source_uri, ('revision', revision)) IN ({live}))"
    )


# A chunk of the document revision `{rev:UInt64}`, or one written before chunks recorded
# their revision (`has_live_chunks_sql` counts those differently, and says why).
OF_REVISION_SQL = (
    "(JSONExtractUInt(chunk_metadata, 'revision') = {rev:UInt64}"
    " OR NOT JSONHas(chunk_metadata, 'revision'))"
)
