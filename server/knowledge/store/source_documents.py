"""Content reads at exact admitted document identities, with no authority fallback."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Any

from server.core import pg
from server.core.ch import get_client, set_query_tag
from server.knowledge.domain.live_chunks import live_chunk_sql
from server.knowledge.domain.serving import Serving
from server.knowledge.store.documents import DOCUMENT_COLUMNS, DocumentRow, _hydrate
from server.knowledge.store.inspect import SCHEME_SQL, format_sql
from server.knowledge.store.pg_documents import decode_row
from server.knowledge.store.source_candidates import candidates

_PG_LIST_CANDIDATES = """
WITH metadata AS (
    SELECT h.namespace COLLATE "C" AS namespace, h.source_uri COLLATE "C" AS source_uri,
        s.original COLLATE "C" AS public_namespace,
        left(h.source_uri, length(h.source_uri) - 37) COLLATE "C" AS public_uri
    FROM loom.knowledge_heads h
    JOIN unnest($2::text[], $3::text[], $4::text[]) AS s(mirror, source, original)
      ON h.namespace = s.mirror AND right(h.source_uri, 37) = '#' || s.source
    WHERE h.org = $1 AND NOT h.deleted AND h.row ->> 'superseded_at' IS NULL
      AND length(h.source_uri) > 37
)
SELECT namespace, source_uri, public_namespace, public_uri FROM metadata
WHERE (public_namespace, public_uri, namespace, source_uri) > ($5, $6, $7, $8)
ORDER BY public_namespace, public_uri, namespace, source_uri LIMIT 64
"""

_TITLE_COLUMNS = (
    "org",
    "namespace",
    "source_uri",
    "version",
    "revision",
    "title",
    "ingested_at",
    "superseded_at",
)

_PG_TITLE_FORMAT = """
coalesce(nullif(row ->> 'format', ''),
    nullif(CASE WHEN jsonb_typeof(meta -> 'ingested_format') = 'string'
        THEN meta ->> 'ingested_format' ELSE '' END, ''),
    nullif(row ->> 'blob_content_type', ''),
    CASE WHEN ext != '' THEN '.' || ext ELSE '(none)' END)
"""

_TITLE_FORMAT_SQL = format_sql(
    "if(JSONType(metadata, 'ingested_format') = 'String', "
    "JSONExtractString(metadata, 'ingested_format'), '')"
)


async def title_projection(
    org: str, namespace: str, source_uri: str, version: int, revision: int
) -> dict[str, Any] | None:
    """Read inspection scalars at one admitted live identity, without bodies or diagnostics."""
    if not pg.enabled():
        return await get_client().query_one(
            f"SELECT {', '.join(_TITLE_COLUMNS)}, {_TITLE_FORMAT_SQL} AS doc_format, "
            f"{SCHEME_SQL} AS source_scheme, "
            "if(JSONType(metadata, 'source_revision') = 'String', "
            "JSONExtractString(metadata, 'source_revision'), '') AS source_revision "
            "FROM loom.documents FINAL WHERE org = {org:String} AND namespace = {ns:String} "
            "AND source_uri = {uri:String} AND version = {version:UInt64} "
            "AND revision = {revision:UInt64} AND superseded_at IS NULL LIMIT 1",
            params={
                "org": org,
                "ns": namespace,
                "uri": source_uri,
                "version": version,
                "revision": revision,
            },
        )
    fields = ", ".join(f"'{col}', row -> '{col}'" for col in _TITLE_COLUMNS)
    saved = await pg.fetchrow(
        "WITH selected AS (SELECT h.row, h.source_uri, "
        "CASE WHEN pg_input_is_valid(h.row ->> 'metadata', 'jsonb') "
        "THEN (h.row ->> 'metadata')::jsonb ELSE '{}'::jsonb END AS meta "
        "FROM loom.knowledge_heads h "
        "WHERE h.org = $1 AND h.namespace = $2 AND h.source_uri = $3 "
        "AND NOT h.deleted AND h.row ->> 'version' = $4 AND h.revision = $5 "
        "AND h.row ->> 'revision' = $6 AND h.row ->> 'superseded_at' IS NULL LIMIT 1), "
        "formatted AS (SELECT row, source_uri, meta, "
        "coalesce(lower(substring(CASE WHEN coalesce(row ->> 'original_filename', '') != '' "
        "THEN row ->> 'original_filename' ELSE "
        "regexp_replace(regexp_replace(source_uri, '^[A-Za-z][A-Za-z0-9+.-]*://[^/]*', ''), "
        "'[?#].*$', '') END from '[.]([A-Za-z0-9]{1,10})$')), '') AS ext FROM selected) "
        f"SELECT jsonb_build_object({fields}, 'doc_format', {_PG_TITLE_FORMAT}, "
        "'source_scheme', coalesce(nullif(lower(substring(source_uri "
        "from '^([A-Za-z][A-Za-z0-9+.-]*)://')), ''), '(none)'), "
        "'source_revision', CASE WHEN jsonb_typeof(meta -> 'source_revision') = 'string' "
        "THEN meta ->> 'source_revision' ELSE '' END) AS row FROM formatted",
        org,
        namespace,
        source_uri,
        str(version),
        revision,
        str(revision),
    )
    return None if saved is None else decode_row(saved["row"])


async def _pg_list_candidates(
    org: str, bindings: list[tuple[str, str]], originals: list[str]
) -> AsyncGenerator[dict[str, Any], None]:
    position: tuple[str, ...] = ("", "", "", "")
    set_query_tag("knowledge.mirror.list_candidates")
    while True:
        # Release the connection before yielding: permission checks use this pool too.
        page = await pg.fetch(
            _PG_LIST_CANDIDATES,
            org,
            [ns for ns, _ in bindings],
            [source for _, source in bindings],
            originals,
            *position,
        )
        for saved in page:
            row = dict(saved)
            next_position = tuple(
                row[k] for k in ("public_namespace", "public_uri", "namespace", "source_uri")
            )
            if next_position <= position:
                raise ValueError("nonadvancing list metadata page")
            position = next_position
            yield row
        if len(page) < 64:
            break


@asynccontextmanager
async def list_candidates(
    org: str, sources: dict[tuple[str, str], str], access_keys: list[str] | None
) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
    """Group metadata by public family before admission, without a candidate cutoff."""
    if not sources:
        raise ValueError("list discovery requires selected sources")
    bindings = sorted(sources)
    originals = [sources[key] for key in bindings]
    if pg.enabled():
        async with aclosing(_pg_list_candidates(org, bindings, originals)) as rows:
            yield rows
        return
    params: dict[str, Any] = {
        "org": org,
        "nss": sorted({ns for ns, _ in bindings}),
        "bindings": bindings,
        "originals": originals,
    }
    coarse = ""
    if access_keys is not None:
        params["acl_keys"] = access_keys
        coarse = " AND hasAny(access_keys, {acl_keys:Array(String)})"
    sql = (
        "SELECT namespace, source_uri, public_namespace, public_uri FROM ("
        "SELECT namespace, source_uri, indexOf({bindings:Array(Tuple(String, String))}, "
        "(namespace, right(source_uri, 36))) AS binding, "
        "arrayElement({originals:Array(String)}, binding) AS public_namespace, "
        "substring(source_uri, 1, greatest(toInt64(length(source_uri)) - 37, 0)) AS public_uri "
        "FROM loom.documents FINAL WHERE org = {org:String} "
        "AND namespace IN {nss:Array(String)} AND superseded_at IS NULL "
        "AND length(source_uri) > 37 AND substring(source_uri, -37, 1) = '#'"
        + coarse
        + ") WHERE binding > 0 "
        "ORDER BY public_namespace, public_uri, namespace, source_uri FORMAT JSONEachRow"
    )
    async with candidates(sql, params) as rows:
        yield rows


async def ordinary_families(
    org: str, pairs: list[tuple[str, str]], *, include_superseded: bool = False
) -> set[tuple[str, str]]:
    """Find ordinary shadows independently of the caller's filters and page position."""
    if len(pairs) > 64:
        raise ValueError("ordinary shadow batch exceeds bound")
    if not pairs:
        return set()
    pairs = sorted(set(pairs))
    live = "" if include_superseded else " AND superseded_at IS NULL"
    rows = await get_client().query_all(
        "SELECT namespace, source_uri FROM loom.documents FINAL WHERE org = {org:String} "
        "AND (namespace, source_uri) IN {pairs:Array(Tuple(String, String))}"
        + live
        + " LIMIT {limit:UInt32}",
        params={"org": org, "pairs": pairs, "limit": len(pairs) + 1},
    )
    if pg.enabled():
        # Keep the ordinary listing's published rows and unpublished heads as shadows.
        rows = [
            *rows,
            *(
                await pg.fetch(
                    "SELECT h.namespace, h.source_uri FROM loom.knowledge_heads h "
                    "JOIN unnest($2::text[], $3::text[]) AS s(namespace, source_uri) "
                    "ON (h.namespace, h.source_uri) = (s.namespace, s.source_uri) WHERE h.org = $1"
                    + (
                        ""
                        if include_superseded
                        else " AND NOT h.deleted AND h.row ->> 'superseded_at' IS NULL"
                    )
                    + " LIMIT $4",
                    org,
                    [ns for ns, _ in pairs],
                    [uri for _, uri in pairs],
                    len(pairs) + 1,
                )
            ),
        ]
    found = {(str(row["namespace"]), str(row["source_uri"])) for row in rows}
    if not found.issubset(pairs):
        raise ValueError("unexpected ordinary shadow identity")
    return found


_LIST_COLUMNS = (
    "org",
    "namespace",
    "source_uri",
    "version",
    "revision",
    "title",
    "description",
    "tags",
    "ingested_at",
    "superseded_at",
    "blob_path",
    "blob_size",
    "blob_content_type",
    "original_filename",
)


async def list_chunk_count(
    org: str, namespace: str, source_uri: str, revision: int, serving: Serving | None
) -> int:
    """Count logical chunks of one admitted copy, including embedding stubs."""
    params: dict[str, Any] = {"org": org, "ns": namespace, "uri": source_uri, "revision": revision}
    version_filter = ""
    if pg.enabled():
        if serving is None or serving.deleted or serving.revision != revision:
            raise ValueError("missing current chunk count proof")
        if serving.chunks_revision != revision or serving.chunks_version is None:
            return 0
        params["version"] = serving.chunks_version
        version_filter = " AND doc_version = {version:UInt64}"
    row = await get_client().query_one(
        "SELECT uniqExact(embedding_model, chunk_idx) AS chunk_count "
        "FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String} "
        "AND source_uri = {uri:String} "
        "AND JSONExtractUInt(chunk_metadata, 'revision') = {revision:UInt64} "
        "AND " + live_chunk_sql() + version_filter + " LIMIT 1",
        params=params,
    )
    if row is None:
        raise ValueError("missing chunk count result")
    return int(row["chunk_count"])


async def list_projection(
    org: str, namespace: str, source_uri: str, version: int, revision: int
) -> dict[str, Any] | None:
    """Read list fields only after admission, at the admitted live version."""
    if not pg.enabled():
        columns = ", ".join(
            "toString(blob_content_type) AS blob_content_type"
            if col == "blob_content_type"
            else col
            for col in (*_LIST_COLUMNS, "content_preview")
        )
        return await get_client().query_one(
            f"SELECT {columns} FROM loom.documents FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String} "
            "AND version = {version:UInt64} AND revision = {revision:UInt64} "
            "AND superseded_at IS NULL LIMIT 1",
            params={
                "org": org,
                "ns": namespace,
                "uri": source_uri,
                "version": version,
                "revision": revision,
            },
        )
    fields = ", ".join(f"'{col}', h.row -> '{col}'" for col in _LIST_COLUMNS)
    # Return only the materialized preview's byte bound, including encoded text.
    saved = await pg.fetchrow(
        f"SELECT jsonb_build_object({fields}) AS row, "
        "encode(substring(CASE WHEN jsonb_typeof(h.row -> 'content') = 'string' "
        "THEN convert_to(h.row ->> 'content', 'UTF8') "
        "WHEN jsonb_typeof(h.row -> 'content') = 'object' "
        "THEN decode(h.row -> 'content' ->> '$loom_base64', 'base64') "
        "ELSE ''::bytea END from 1 for 220), 'base64') AS preview "
        "FROM loom.knowledge_heads h "
        "WHERE h.org = $1 AND h.namespace = $2 AND h.source_uri = $3 "
        "AND NOT h.deleted AND h.row ->> 'version' = $4 AND h.revision = $5 "
        "AND h.row ->> 'revision' = $6 AND h.row ->> 'superseded_at' IS NULL LIMIT 1",
        org,
        namespace,
        source_uri,
        str(version),
        revision,
        str(revision),
    )
    if saved is None:
        return None
    row = decode_row(saved["row"])
    row["content_preview"] = base64.b64decode(saved["preview"] or "").decode("utf-8", "replace")
    return row


@asynccontextmanager
async def chunk_candidates(
    org: str, namespace: str, source_uri: str, model: str
) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
    """Stream one admitted document's live chunk identities, including embedding stubs."""
    sql = (
        "SELECT namespace, source_uri, chunk_idx, doc_version, "
        "JSONExtractUInt(chunk_metadata, 'revision') AS revision "
        "FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String} "
        "AND source_uri = {uri:String} AND embedding_model = {model:String} "
        "AND " + live_chunk_sql() + " ORDER BY chunk_idx, doc_version DESC FORMAT JSONEachRow"
    )
    async with candidates(
        sql, {"org": org, "ns": namespace, "uri": source_uri, "model": model}
    ) as rows:
        yield rows


async def contents(
    org: str, namespace: str, source_uri: str, version: int, revision: int
) -> DocumentRow | None:
    if pg.enabled():
        saved = await pg.fetchrow(
            "SELECT h.row FROM loom.knowledge_heads h "
            "WHERE h.org = $1 AND h.namespace = $2 AND h.source_uri = $3 "
            "AND NOT h.deleted AND h.row ->> 'version' = $4 AND h.revision = $5 "
            "AND h.row ->> 'revision' = $6 LIMIT 1",
            org,
            namespace,
            source_uri,
            str(version),
            revision,
            str(revision),
        )
        row = decode_row(saved["row"]) if saved else None
    else:
        columns = ", ".join(
            f"toString({col}) AS {col}"
            if col in {"blob_content_type", "source_class", "format"}
            else col
            for col in DOCUMENT_COLUMNS
        )
        row = await get_client().query_one(
            f"SELECT {columns} FROM loom.documents FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String} "
            "AND version = {version:UInt64} AND revision = {revision:UInt64} "
            "AND superseded_at IS NULL LIMIT 1",
            params={
                "org": org,
                "ns": namespace,
                "uri": source_uri,
                "version": version,
                "revision": revision,
            },
        )
    return _hydrate(row) if row is not None and row.get("superseded_at") is None else None
