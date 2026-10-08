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
