"""Unit tests for server/knowledge/app/search.py."""

from __future__ import annotations

from typing import Any

import pytest

from server.knowledge.app.search import ChunkHit, HybridSearchQuery, hybrid_search


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []


def _row() -> dict[str, Any]:
    return {
        "namespace": "ns",
        "source_uri": "kb://policy.md",
        "chunk_idx": 0,
        "doc_version": 11,
        "content": "Deploy Tue/Thu.",
        "context": "Runbook\nOps > Deploys",
        "tags": ["policy"],
        "chunk_metadata": '{"section_path": ["Ops", "Deploys"], "section_depth": 2}',
        "distance": 0.21,
    }


def _q(**overrides) -> HybridSearchQuery:
    base = {
        "org": "clickhouse",
        "namespaces": ("ns",),
        "embedding_model": "text-embedding-3-small",
        "query_vec": [0.1] * 1536,
        "top_k": 10,
    }
    base.update(overrides)
    return HybridSearchQuery(**base)


@pytest.mark.asyncio
async def test_hybrid_search_emits_canonical_filters() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_row()])
    out = await hybrid_search(_q(), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "doc_superseded_at IS NULL" in sql
    assert "embedding_dim > 0" in sql
    assert "embedding_model = {model:String}" in sql
    assert "ORDER BY distance ASC" in sql
    assert params["model"] == "text-embedding-3-small"
    assert len(out) == 1 and isinstance(out[0], ChunkHit)
    # Which write inserted the chunk: what the Postgres authority's check reads.
    assert "doc_version," in sql and out[0].doc_version == 11
    assert "context," in sql and out[0].context == "Runbook\nOps > Deploys"


@pytest.mark.asyncio
async def test_tag_filter_applies_hasany() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await hybrid_search(_q(tags=["sec", "policy"]), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "hasAny(tags, {tags:Array(String)})" in sql
    assert params["tags"] == ["sec", "policy"]


@pytest.mark.asyncio
async def test_no_tags_omits_hasany_clause() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await hybrid_search(_q(), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "hasAny(tags" not in sql
    assert "tags" not in params


@pytest.mark.asyncio
async def test_recency_cutoff_filters_ingested_at() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await hybrid_search(_q(since_seconds=3600), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "ingested_at >= {since:DateTime}" in sql
    assert "since" in params


@pytest.mark.asyncio
async def test_hits_hydrate_with_typed_int_chunk_idx() -> None:
    """Defensive: chunk_idx from CH could be UInt32 (cast); hydration
    forces Python int so downstream comparisons don't surprise."""
    ch = StubCH()
    ch.query_all_returns.append([{**_row(), "chunk_idx": 5}])
    out = await hybrid_search(_q(), ch=ch)
    assert isinstance(out[0].chunk_idx, int)
    assert out[0].chunk_idx == 5


@pytest.mark.asyncio
async def test_hits_carry_the_section_breadcrumb_and_tolerate_missing_metadata() -> None:
    ch = StubCH()
    legacy = _row()
    legacy.pop("chunk_metadata")
    ch.query_all_returns.append([_row(), legacy, {**_row(), "chunk_metadata": "not json"}])
    out = await hybrid_search(_q(), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "chunk_metadata," in sql
    assert out[0].section_path == ("Ops", "Deploys") and out[0].section == "Ops > Deploys"
    assert out[1].section_path == () and out[1].section == ""
    assert out[2].section_path == ()


@pytest.mark.asyncio
async def test_uri_prefix_scope_is_a_startswith_range() -> None:
    """Each prefix becomes startsWith(source_uri, …) over the
    sort-key column; several prefixes are OR-ed; empty strings are dropped."""
    ch = StubCH()
    ch.query_all_returns.append([_row()])
    await hybrid_search(_q(uri_prefixes=["kb://ops/", "", "kb://policy"]), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "(startsWith(source_uri, {pfx0:String}) OR startsWith(source_uri, {pfx1:String}))" in sql
    assert params["pfx0"] == "kb://ops/" and params["pfx1"] == "kb://policy"
    assert "pfx2" not in params


@pytest.mark.asyncio
async def test_no_uri_prefix_means_no_scope_clause() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_row()])
    await hybrid_search(_q(), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "startsWith(source_uri" not in sql
