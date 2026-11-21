"""Metadata discovery streams; content is fetched only at admitted chunk keys."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Any, Literal

from server.core import bm25
from server.core.ch import CHClient, get_client, set_query_tag
from server.knowledge.domain.live_chunks import live_chunk_sql

Leg = Literal["semantic", "lexical", "keys"]
ChunkKey = tuple[str, str, int, int, int]


async def chunks(
    org: str,
    model: str,
    keys: list[ChunkKey],
    *,
    ch: CHClient | None = None,
    embedded_only: bool = True,
) -> list[dict[str, Any]]:
    """Fetch one bounded batch at its admitted key, never a replacement version."""
    if keys:
        set_query_tag("knowledge.mirror.hydrate")
    return await _read_chunks(
        org,
        model,
        keys,
        "content, context, tags, chunk_metadata, ingested_at, agent, session_id, "
        "toString(source_class) AS source_class",
        ch=ch,
        embedded_only=embedded_only,
    )


async def chunk_keys(
    org: str, model: str, keys: list[ChunkKey], *, embedded_only: bool = True
) -> list[ChunkKey]:
    """Probe exact live keys without transferring content or context."""
    if keys:
        set_query_tag("knowledge.mirror.search_proof")
    rows = await _read_chunks(org, model, keys, "", embedded_only=embedded_only)
    return [
        (
            row["namespace"],
            row["source_uri"],
            int(row["chunk_idx"]),
            int(row["doc_version"]),
            int(row["revision"]),
        )
        for row in rows
    ]


async def _read_chunks(
    org: str,
    model: str,
    keys: list[ChunkKey],
    fields: str,
    *,
    ch: CHClient | None = None,
    embedded_only: bool = True,
) -> list[dict[str, Any]]:
    if not keys:
        return []
    if len(keys) > 64:
        raise ValueError("chunk hydration batch exceeds bound")
    keys = sorted(set(keys))
    return await (ch or get_client()).query_all(
        "SELECT namespace, source_uri, chunk_idx, doc_version, "
        "JSONExtractUInt(chunk_metadata, 'revision') AS revision"
        + (", " + fields if fields else "")
        + " FROM loom.chunks WHERE org = {org:String} "
        "AND namespace IN {nss:Array(String)} AND embedding_model = {model:String} "
        + ("AND embedding_dim > 0 " if embedded_only else "")
        + "AND "
        + live_chunk_sql()
        + " "
        "AND (namespace, source_uri, chunk_idx, doc_version, "
        "JSONExtractUInt(chunk_metadata, 'revision')) "
        "IN {keys:Array(Tuple(String, String, UInt32, UInt64, UInt64))} "
        "LIMIT {limit:UInt32}",
        params={
            "org": org,
            "nss": sorted({key[0] for key in keys}),
            "model": model,
            "keys": keys,
            "limit": len(keys) + 1,
        },
    )


def statement(
    where: list[str],
    params: dict[str, Any],
    *,
    leg: Leg,
    access_keys: list[str] | None,
) -> tuple[str, dict[str, Any]]:
    """Use trusted shared chunk filters; a Postgres head never depends on CH ACL keys."""
    where, params = list(where), dict(params)
    if access_keys is not None:
        params["acl_keys"] = access_keys
        where.append(
            "(namespace, source_uri) IN (SELECT namespace, source_uri "
            "FROM loom.documents FINAL WHERE org = {org:String} "
            "AND namespace IN {nss:Array(String)} AND superseded_at IS NULL "
            "AND hasAny(access_keys, {acl_keys:Array(String)}))"
        )
    prelude, score, matched = "", "0.0", "[]"
    order = "distance ASC"
    if leg == "lexical":
        params["idfs"] = [1.0] * len(params["toks"])
        params["avgdl"] = 256.0
        prelude = (
            "WITH arrayConcat(splitByNonAlpha(context_lc), splitByNonAlpha(content_lc)) AS words"
        )
        score = bm25.bm25_sum_sql(
            bm25.tf_array_sql("countEqual(words, tok)", "{toks:Array(String)}"),
            "{idfs:Array(Float32)}",
            dl="length(words)",
            avgdl="{avgdl:Float32}",
        )
        where.extend(
            [
                "(hasAnyTokens(content_lc, {toks:Array(String)}) "
                "OR hasAnyTokens(context_lc, {toks:Array(String)}))",
                "content != ''",
            ]
        )
        order = "lexical DESC, distance ASC"
    elif leg == "keys":
        where.append("hasAny(keys, {keys:Array(String)})")
        matched = "arrayIntersect(keys, {keys:Array(String)})"
        order = "length(matched) DESC, distance ASC"
    elif leg != "semantic":
        raise ValueError("unknown candidate leg")
    return (
        f"""{prelude}
        SELECT namespace, source_uri, chunk_idx, doc_version,
            JSONExtractUInt(chunk_metadata, 'revision') AS revision,
            cosineDistance(embedding, {{q:Array(Float32)}}) AS distance,
            {score} AS lexical, {matched} AS matched
        FROM loom.chunks WHERE {" AND ".join(where)}
        ORDER BY {order}, namespace, source_uri, chunk_idx, doc_version DESC
        SETTINGS query_plan_direct_read_from_text_index = 0
        FORMAT JSONEachRow""",
        params,
    )


async def _rows(body: AsyncIterator[bytes]) -> AsyncGenerator[dict[str, Any], None]:
    pending = b""
    async for block in body:
        lines = (pending + block).split(b"\n")
        pending = lines.pop()
        for line in lines:
            if line:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("invalid candidate row")
                yield value
    if pending:
        value = json.loads(pending)
        if not isinstance(value, dict):
            raise ValueError("invalid candidate row")
        yield value


@asynccontextmanager
async def candidates(
    sql: str, params: dict[str, Any], *, ch: CHClient | None = None
) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
    set_query_tag("knowledge.mirror.candidates")
    async with (ch or get_client()).raw_stream(sql, params) as body:
        async with aclosing(_rows(body)) as rows:
            yield rows
