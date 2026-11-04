"""The reads behind `knowledge.inspect`: a namespace's documents grouped by format, source
scheme and state, its live chunks by embedding model, the documents whose chunks search
cannot serve, and, for the newest documents, their chunks and retired versions.

Every statement filters on (org, namespace), the prefix of each table's sort key.
`loom.documents` holds one row per document, its current version, read with FINAL; an
earlier version is known only through its chunks, which stay in `loom.chunks` and are
retired by a `loom.chunk_supersessions` row. Documents are the namespace's under any
read scope, as every `knowledge.*` read serves them. No statement reads a document's
text; the per-document chunk read measures the listed documents' chunk text only.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

# The most format and scheme pairs the grouping returns.
MAX_GROUPS = 1000  # not a setting: a bound on one report's rows, flagged when reached
# The most sample URIs one check collects; a request's sample_limit cuts it further.
MAX_SAMPLES = 100  # not a setting: sample_limit (at most 100) is the caller's knob

_NONE = "'(none)'"
# A source_uri's scheme: the letters before "://", lowercased; '(none)' for a path.
_SCHEME = "lower(extract(source_uri, '^([A-Za-z][A-Za-z0-9+.-]*)://'))"
SCHEME_SQL = f"if({_SCHEME} != '', {_SCHEME}, {_NONE})"
# The path a file extension is read from: the original filename when one was uploaded,
# else the source_uri without its scheme, host, query and fragment.
_PATH = (
    "if(original_filename != '', original_filename, replaceRegexpOne(replaceRegexpOne("
    "source_uri, '^[A-Za-z][A-Za-z0-9+.-]*://[^/]*', ''), '[?#].*$', ''))"
)
_EXT = f"lower(extract({_PATH}, '[.]([A-Za-z0-9]{{1,10}})$'))"
# The format the row records (knowledge.ingest sets it, knowledge.get serves it), else
# the format a source sync converted it from (metadata `ingested_format`: transcript,
# markdown, slides, or the extractor's pdf or pptx), else the stored content type, else
# ".<extension>", else '(none)'.
_SYNCED = "JSONExtractString(metadata, 'ingested_format')"
FORMAT_SQL = (
    f"if(format != '', toString(format), if({_SYNCED} != '', {_SYNCED}, "
    f"if(blob_content_type != '', toString(blob_content_type), "
    f"if({_EXT} != '', concat('.', {_EXT}), {_NONE}))))"
)

_DOCS = "FROM loom.documents FINAL WHERE org = {org:String} AND namespace = {ns:String}"
_LIVE_CHUNKS = (
    f"FROM loom.chunks WHERE org = {{org:String}} AND namespace = {{ns:String}} "
    f"AND {LIVE_CHUNK_SQL}"
)


async def document_groups(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """(doc_format, source_scheme, live, deleted, newest live ingest, earlier revisions,
    unnumbered rows) per pair, at most MAX_GROUPS + 1: one more than MAX_GROUPS means the
    pairs were cut. A row's `revision` numbers its published revisions (1, 2, ...), so
    `revision - 1` earlier ones were published; a row no write numbered reads 0."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT {FORMAT_SQL} AS doc_format, {SCHEME_SQL} AS source_scheme,
               countIf(superseded_at IS NULL) AS live,
               countIf(superseded_at IS NOT NULL) AS deleted,
               maxIf(ingested_at, superseded_at IS NULL) AS newest,
               sumIf(revision - 1, revision > 0) AS earlier_revisions,
               countIf(revision = 0) AS unnumbered
        {_DOCS}
        GROUP BY doc_format, source_scheme
        ORDER BY live DESC, deleted DESC, doc_format, source_scheme
        LIMIT {{cap:UInt32}}
        """,
        # One past the cap, so a full read is told apart from a cut one.
        params={"org": org, "ns": namespace, "cap": MAX_GROUPS + 1},
    )


async def retired_versions(org: str, namespace: str, *, ch: _CHClient | None = None) -> int:
    """Distinct (document, chunk version) pairs whose chunks were retired, across models:
    the revisions a later write or a delete replaced, and every knowledge.ingest attempt
    that inserted chunks and never published a document row (failed, cancelled, or beaten
    by a later write), which no revision numbers."""
    row = await (ch or _default_client()).query_one(
        "SELECT uniqExact(source_uri, doc_version) AS n FROM loom.chunk_supersessions "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
    )
    return int(row["n"]) if row else 0


async def chunk_models(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """(model, live, embedded) over the namespace's live chunks, most first: one row per
    embedding model the namespace was written under."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT toString(embedding_model) AS model, count() AS live,
               countIf(embedding_dim > 0) AS embedded
        {_LIVE_CHUNKS}
        GROUP BY model
        ORDER BY live DESC, model
        """,
        params={"org": org, "ns": namespace},
    )


async def chunk_health(
    org: str, namespace: str, model: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """Over the live documents: how many there are, and how many (with up to MAX_SAMPLES
    URIs each) have no live chunk, a live chunk with no vector, live chunks none of which
    is under `model`, or live chunks of more than one version under `model`. The
    documents are filtered in a derived table before the join (the knowledge.list read
    does the same), and a missing chunk side reads as 0 under either `join_use_nulls`."""
    n = MAX_SAMPLES
    checks = {
        "without_chunks": "ifNull(c.live, 0) = 0",
        "unembedded": "ifNull(c.unembedded, 0) > 0",
        "other_model": "ifNull(c.live, 0) > 0 AND ifNull(c.current, 0) = 0",
        "mixed_versions": "ifNull(c.versions, 0) > 1",
    }
    # Per check: the matches, their URIs, and the ones knowledge.ingest published (a text
    # write would turn those into text documents), with theirs.
    ingested = "d.ingestion_id != ''"
    columns = ",\n".join(
        f"countIf({cond}) AS {name}, groupArrayIf({n})(d.source_uri, {cond}) AS {name}_uris, "
        f"countIf({cond} AND {ingested}) AS {name}_ingested, "
        f"groupArrayIf({n})(d.source_uri, {cond} AND {ingested}) AS {name}_ingested_uris"
        for name, cond in checks.items()
    )
    return await (ch or _default_client()).query_one(
        f"""
        SELECT count() AS live_docs,
               {columns}
        FROM (
            SELECT source_uri, ingestion_id {_DOCS} AND superseded_at IS NULL
        ) AS d
        LEFT JOIN (
            SELECT source_uri, count() AS live,
                   countIf(embedding_model = {{model:String}}) AS current,
                   countIf(embedding_dim = 0) AS unembedded,
                   uniqExactIf(doc_version, embedding_model = {{model:String}}) AS versions
            {_LIVE_CHUNKS}
            GROUP BY source_uri
        ) AS c ON d.source_uri = c.source_uri
        """,
        params={"org": org, "ns": namespace, "model": model},
    )


async def newest_documents(
    org: str, namespace: str, *, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The newest live documents, at most `limit`, ties broken by source_uri, with their
    revision and the ingestion that published them ('' for a text write)."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT source_uri, title, version, revision, ingestion_id, ingested_at,
               {FORMAT_SQL} AS doc_format, {SCHEME_SQL} AS source_scheme
        {_DOCS} AND superseded_at IS NULL
        ORDER BY ingested_at DESC, source_uri DESC
        LIMIT {{lim:UInt32}}
        """,
        params={"org": org, "ns": namespace, "lim": limit},
    )


async def document_chunks(
    org: str, namespace: str, source_uris: list[str], model: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Per listed document, its live chunks: counts by state, versions under `model`,
    characters per chunk (min, median, max) and the models they were written under."""
    if not source_uris:
        return []
    return await (ch or _default_client()).query_all(
        f"""
        SELECT source_uri, count() AS live,
               countIf(embedding_model = {{model:String}} AND embedding_dim > 0) AS searchable,
               countIf(embedding_dim = 0) AS unembedded,
               countIf(embedding_model != {{model:String}}) AS other_model,
               uniqExactIf(doc_version, embedding_model = {{model:String}}) AS versions,
               min(lengthUTF8(content)) AS min_chars,
               toUInt64(quantileExact(0.5)(lengthUTF8(content))) AS median_chars,
               max(lengthUTF8(content)) AS max_chars,
               arraySort(groupUniqArray(toString(embedding_model))) AS models
        {_LIVE_CHUNKS} AND source_uri IN {{uris:Array(String)}}
        GROUP BY source_uri
        """,
        params={"org": org, "ns": namespace, "uris": source_uris, "model": model},
    )


async def document_retired_versions(
    org: str, namespace: str, source_uris: list[str], *, ch: _CHClient | None = None
) -> dict[str, int]:
    """source_uri -> distinct versions whose chunks were retired, for the listed ones."""
    if not source_uris:
        return {}
    rows = await (ch or _default_client()).query_all(
        """
        SELECT source_uri, uniqExact(doc_version) AS n
        FROM loom.chunk_supersessions
        WHERE org = {org:String} AND namespace = {ns:String}
          AND source_uri IN {uris:Array(String)}
        GROUP BY source_uri
        """,
        params={"org": org, "ns": namespace, "uris": source_uris},
    )
    return {str(r["source_uri"]): int(r["n"]) for r in rows}


# The most longer URIs `longer_document_uris` returns.
MAX_LONGER = 1000  # not a setting: a bound on one report's rows, flagged when reached


async def longer_document_uris(
    org: str, namespace: str, source_uris: list[str], *, ch: _CHClient | None = None
) -> list[str]:
    """The documents, live or deleted, whose source_uri extends one of `source_uris` with
    `#` (`doc#v2` for `doc`), at most MAX_LONGER + 1 (one more means cut): a citation of
    `doc#v2#Intro` names `doc#v2`, not `doc`, as `loom decisions extract` reads it. Reads
    the sort-key column."""
    if not source_uris:
        return []
    rows = await (ch or _default_client()).query_all(
        f"""
        SELECT DISTINCT source_uri {_DOCS}
          AND arrayExists(u -> startsWith(source_uri, concat(u, '#')), {{uris:Array(String)}})
        ORDER BY source_uri
        LIMIT {{cap:UInt32}}
        """,
        params={"org": org, "ns": namespace, "uris": source_uris, "cap": MAX_LONGER + 1},
    )
    return [str(r["source_uri"]) for r in rows]
