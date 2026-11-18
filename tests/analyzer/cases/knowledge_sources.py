"""Checked source metadata, discovery and exact-identity read statements."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

from server.core import pg
from server.knowledge.app.search import HybridSearchQuery, _common_where
from server.knowledge.domain.serving import Serving
from server.knowledge.store import source_access, source_candidates, source_documents
from tests.analyzer.cases._common import MODEL, NS, ORG, SMALL_VEC, rows_by_substring
from tests.analyzer.corpus import Case, RecordingCH

_MIRROR = "acl-" + "a" * 24
_SOURCE = "00000000-0000-0000-0000-000000000001"
_PUBLIC_URI = "doc://a"
_URI = _PUBLIC_URI + "#" + _SOURCE
_CHUNK = (_MIRROR, _URI, 0, 10, 1)


async def _access(ch: RecordingCH, _c: dict[str, Any]) -> None:
    await source_access.active_sources(ORG, [NS], ch=ch)
    await source_access.listings(ORG, [(_MIRROR, _SOURCE)], ch=ch)
    await source_access.metadata(ORG, [(_MIRROR, _URI)], ch=ch)
    await source_access.group_members(ORG, [(_MIRROR, _SOURCE, "group")], ch=ch)


async def _chunks(ch: RecordingCH, c: dict[str, Any]) -> None:
    await source_candidates.chunks(ORG, MODEL, [_CHUNK], ch=ch, embedded_only=c["embedded_only"])
    await source_candidates.chunk_keys(ORG, MODEL, [_CHUNK])


async def _candidates(ch: RecordingCH, c: dict[str, Any]) -> None:
    query = HybridSearchQuery(
        org=ORG,
        namespaces=(_MIRROR,),
        embedding_model=MODEL,
        query_vec=SMALL_VEC,
        top_k=10,
        tokens=["deploy"],
        keys=["deploy_window"],
    )
    where, params = _common_where(query)
    where.append("hasAny(tags, {source_tags:Array(String)})")
    params.update(source_tags=["source-id:" + _SOURCE], toks=query.tokens, keys=query.keys)
    sql, params = source_candidates.statement(
        where, params, leg=c["leg"], access_keys=["owner:reader"] if c["coarse"] else None
    )
    await ch.query_all(sql, params)


async def _documents(_ch: RecordingCH, c: dict[str, Any]) -> None:
    await source_documents.title_projection(ORG, _MIRROR, _URI, 10, 1)
    await source_documents.list_projection(ORG, _MIRROR, _URI, 10, 1)
    await source_documents.contents(ORG, _MIRROR, _URI, 10, 1)
    await source_documents.ordinary_families(
        ORG, [(NS, _PUBLIC_URI)], include_superseded=c["include_superseded"]
    )


async def _discovery(ch: RecordingCH, c: dict[str, Any]) -> None:
    # Capture the real builders at their stream adapter; the recorder has no raw_stream.
    @asynccontextmanager
    async def capture(
        sql: str, params: dict[str, Any]
    ) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        recorded = await ch.query_all(sql, params)

        async def rows() -> AsyncIterator[dict[str, Any]]:
            for row in recorded:
                yield row

        yield rows()

    with patch.object(source_documents, "candidates", capture):
        async with source_documents.list_candidates(
            ORG, {(_MIRROR, _SOURCE): NS}, ["owner:reader"] if c["coarse"] else None
        ) as rows:
            async for _ in rows:
                pass
        async with source_documents.chunk_candidates(ORG, _MIRROR, _URI, MODEL) as rows:
            async for _ in rows:
                pass


async def _chunk_count(_ch: RecordingCH, c: dict[str, Any]) -> None:
    # Both branches issue CH SQL; the served-version branch does not read a PG head.
    served = c["served_version"]
    with patch.object(pg, "enabled", return_value=served):
        await source_documents.list_chunk_count(
            ORG, _MIRROR, _URI, 1, Serving(False, 1, 1, 10) if served else None
        )


def cases() -> list[Case]:
    return [
        Case("mirror_access_reads", source_access.__name__, _access, {}),
        Case(
            "mirror_chunk_reads",
            source_candidates.__name__,
            _chunks,
            {"embedded_only": (False, True)},
        ),
        Case(
            "mirror_candidate_statements",
            source_candidates.__name__,
            _candidates,
            {"leg": ("semantic", "lexical", "keys"), "coarse": (False, True)},
        ),
        Case(
            "mirror_document_reads",
            source_documents.__name__,
            _documents,
            {"storage_authority": ("clickhouse",), "include_superseded": (False, True)},
        ),
        Case(
            "mirror_document_discovery",
            source_documents.__name__,
            _discovery,
            {"storage_authority": ("clickhouse",), "coarse": (False, True)},
        ),
        Case(
            "mirror_document_chunk_count",
            source_documents.__name__,
            _chunk_count,
            {"served_version": (False, True)},
            rows=rows_by_substring(
                {"SELECT uniqExact(embedding_model, chunk_idx)": [{"chunk_count": 0}]}
            ),
        ),
    ]
