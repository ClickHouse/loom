"""knowledge.search lexical leg: tokenisation matches the
view, BM25 math, the SQL shape, reciprocal-rank fusion, and the handler's
mode switch with a cold-namespace fallback."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from server.knowledge.app.search import ChunkHit, HybridSearchQuery, fuse_rrf, lexical_search


class StubCH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        return self.rows

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        return self.rows[0] if self.rows else None


def test_tokenize_matches_the_view_tokeniser() -> None:
    from server.knowledge.app.bm25 import tokenize

    # lower-cased splitByNonAlpha runs of >= 3 bytes, de-duplicated in order
    assert tokenize("Deploy on Tue/Thu; re-deploy 2x? DEPLOY!") == ["deploy", "tue", "thu"]
    assert tokenize("a bb ccc") == ["ccc"]
    assert tokenize("") == [] and tokenize("--- !!") == []
    # digits stay inside the token, as in the view: s3bucket is one term
    assert tokenize("the s3bucket on k8s, 123") == ["the", "s3bucket", "k8s", "123"]


def _q(**kw: Any) -> HybridSearchQuery:
    base: dict[str, Any] = {
        "org": "o",
        "namespaces": ("ns",),
        "embedding_model": "m",
        "query_vec": [0.1] * 4,
        "top_k": 5,
        "tokens": ["deploy", "rollback"],
        "idfs": [1.5, 2.5],
        "avgdl": 120.0,
    }
    base.update(kw)
    return HybridSearchQuery(**base)


@pytest.mark.asyncio
async def test_lexical_search_sql_shape_and_cold_corpus_short_circuit() -> None:
    ch = StubCH(
        rows=[
            {
                "namespace": "ns",
                "source_uri": "kb://a",
                "chunk_idx": 2,
                "content": "Deploy then rollback",
                "tags": [],
                "chunk_metadata": '{"section_path": ["Ops"]}',
                "distance": 0.4,
                "lexical": 3.2,
            }
        ]
    )
    hits = await lexical_search(_q(), ch=ch)
    sql, params = ch.calls[0]
    # token-based tf, as df is; the context and the text are split once and dl is
    # their word count
    assert sql.startswith(
        "WITH arrayConcat(splitByNonAlpha(context_lc), splitByNonAlpha(content_lc)) AS words SELECT"
    )
    assert sql.count("splitByNonAlpha") == 2
    assert "arrayMap(tok -> countEqual(words, tok), {toks:Array(String)})" in sql
    assert "* length(words) / {avgdl:Float32}" in sql and "length(content)" not in sql
    assert "PREWHERE" not in sql
    # the text index predicates: a token in the context or the text makes a candidate
    assert (
        "(hasAnyTokens(content_lc, {toks:Array(String)})"
        " OR hasAnyTokens(context_lc, {toks:Array(String)}))" in sql
    )
    assert "content != ''" in sql  # an empty chunk is not scored on its context alone
    assert "query_plan_direct_read_from_text_index = 0" in sql
    assert "positionCaseInsensitive" not in sql and "countSubstrings" not in sql
    assert "ORDER BY lexical DESC, distance ASC" in sql and "doc_superseded_at IS NULL" in sql
    assert params["toks"] == ["deploy", "rollback"] and params["idfs"] == [1.5, 2.5]
    assert params["avgdl"] == 120.0
    assert hits[0].lexical_score == 3.2 and hits[0].score == 3.2 and hits[0].section == "Ops"
    assert hits[0].distance == 0.4  # the lexical leg carries the cosine too
    # cold namespace / no tokens: no query issued
    assert await lexical_search(_q(avgdl=0.0), ch=StubCH()) == []
    assert await lexical_search(_q(tokens=[]), ch=StubCH()) == []
    assert await lexical_search(_q(idfs=[1.0]), ch=StubCH()) == []  # misaligned idfs


def _hit(uri: str, idx: int, *, distance: float, lex: float = 0.0) -> ChunkHit:
    return ChunkHit(
        namespace="ns",
        source_uri=uri,
        chunk_idx=idx,
        content=uri,
        tags=[],
        distance=distance,
        lexical_score=lex,
        score=0.0,
    )


def test_fuse_rrf_rewards_agreement_and_tolerates_single_leg_hits() -> None:
    sem = [_hit("a", 0, distance=0.10), _hit("b", 0, distance=0.20), _hit("c", 0, distance=0.30)]
    lex = [
        _hit("b", 0, distance=0.20, lex=5.0),
        _hit("d", 0, distance=0.90, lex=4.0),
        _hit("a", 0, distance=0.10, lex=1.0),
    ]
    fused = fuse_rrf(sem, lex, top_k=10)
    order = [h.source_uri for h in fused]
    # b: sem rank 2 + lex rank 1 beats a: sem rank 1 + lex rank 3
    assert order[:2] == ["b", "a"]
    assert set(order) == {"a", "b", "c", "d"}
    by = {h.source_uri: h for h in fused}
    assert math.isclose(by["b"].score, 1 / 62 + 1 / 61)
    assert math.isclose(by["c"].score, 1 / 63)  # semantic-only hit keeps one term
    assert math.isclose(by["d"].score, 1 / 62)  # lexical-only hit keeps one term
    assert by["b"].lexical_score == 5.0 and by["b"].distance == 0.20
    # a lexical hit with zero BM25 contributes no lexical term
    fused = fuse_rrf(sem, [_hit("c", 0, distance=0.30, lex=0.0)], top_k=10)
    assert math.isclose({h.source_uri: h for h in fused}["c"].score, 1 / 63)
    assert len(fuse_rrf(sem, lex, top_k=2)) == 2
    # deterministic: same inputs, same order
    assert [h.source_uri for h in fuse_rrf(sem, lex, top_k=10)] == order


def test_handler_modes_and_cold_namespace_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace as NS

    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.knowledge.app import knowledge_search as h

    calls: dict[str, int] = {"sem": 0, "lex": 0}

    async def _embed(text: str) -> list[float]:
        return [0.1] * 4

    monkeypatch.setattr(h, "get_embedder", lambda: NS(model_name="m", embed=_embed))

    async def _sem(q: Any, **kw: Any) -> list[ChunkHit]:
        calls["sem"] += 1
        return [_hit("a", 0, distance=0.1), _hit("b", 0, distance=0.3)]

    async def _lex(q: Any, **kw: Any) -> list[ChunkHit]:
        calls["lex"] += 1
        return [_hit("b", 0, distance=0.3, lex=4.0)] if q.avgdl > 0 else []

    monkeypatch.setattr("server.knowledge.app.search.hybrid_search", _sem)
    monkeypatch.setattr("server.knowledge.app.search.lexical_search", _lex)

    async def _stats(ch: Any, **kw: Any) -> tuple[int, float]:
        return 50, 130.0

    async def _idfs(ch: Any, **kw: Any) -> list[float]:
        return [1.0] * len(kw["tokens"])

    monkeypatch.setattr(knowledge_bm25, "corpus_stats", _stats)
    monkeypatch.setattr(knowledge_bm25, "idfs_for", _idfs)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())

    inp = KnowledgeSearchInput(
        org="o", namespace="ns", query="deploy rollback", namespace_scope="this"
    )
    out = asyncio.run(h.handle_knowledge_search(inp))
    assert calls == {"sem": 1, "lex": 1}
    # b appears in both legs -> fused ahead of a (semantic-only)
    assert [r.source_uri for r in out.results] == ["b", "a"]
    assert out.results[0].lexical_score == 4.0 and out.results[0].score > out.results[1].score

    out = asyncio.run(h.handle_knowledge_search(inp.model_copy(update={"search_mode": "semantic"})))
    assert calls == {"sem": 2, "lex": 1}
    assert [r.source_uri for r in out.results] == ["a", "b"] and out.results[
        0
    ].score == out.results[0].similarity

    out = asyncio.run(h.handle_knowledge_search(inp.model_copy(update={"search_mode": "lexical"})))
    assert [r.source_uri for r in out.results] == ["b"] and calls["lex"] == 2

    # Cold namespace: stats say 0 chunks -> lexical leg returns [] -> semantic order stands
    async def _cold(ch: Any, **kw: Any) -> tuple[int, float]:
        return 0, 0.0

    monkeypatch.setattr(knowledge_bm25, "corpus_stats", _cold)
    out = asyncio.run(h.handle_knowledge_search(inp))
    assert [r.source_uri for r in out.results] == ["a", "b"]


# --- section collapse + telemetry registration --------------------------------


def test_collapse_by_section_keeps_the_best_chunk_per_section_and_counts_siblings() -> None:
    from server.knowledge.api.models_knowledge import _SearchHit
    from server.knowledge.app.knowledge_search import _collapse_by_section

    def _h(uri: str, idx: int, path: list[str], score: float, ns: str = "ns") -> _SearchHit:
        return _SearchHit(
            source_uri=uri,
            chunk_idx=idx,
            content="",
            tags=[],
            distance=1 - score,
            similarity=score,
            namespace=ns,
            section_path=path,
            section=" > ".join(path),
            score=score,
        )

    hits = [
        _h("a", 0, ["Ops", "Deploy"], 0.9),
        _h("a", 1, ["Ops", "Deploy"], 0.8),  # sibling of the first -> folded
        _h("b", 0, [], 0.75),  # headless: never folded
        _h("a", 2, ["Ops", "Rollback"], 0.7),  # different section -> kept
        _h("b", 1, [], 0.65),  # headless sibling: kept
        _h("a", 3, ["Ops", "Deploy"], 0.6),  # another sibling -> folded
        _h("a", 0, ["Ops", "Deploy"], 0.5, ns="other"),  # same doc+section, other namespace: kept
    ]
    out = _collapse_by_section(hits)
    assert [(h.source_uri, h.chunk_idx, h.namespace) for h in out] == [
        ("a", 0, "ns"),
        ("b", 0, "ns"),
        ("a", 2, "ns"),
        ("b", 1, "ns"),
        ("a", 0, "other"),
    ]
    assert out[0].siblings_folded == 2 and out[2].siblings_folded == 0


def test_knowledge_search_is_a_registered_search_op_with_a_cosine_plane() -> None:
    from server.knowledge.api.models_knowledge import _SearchHit
    from server.observability import metrics

    assert "knowledge.search" in metrics._SEARCH_OPS
    assert "knowledge" in metrics._COSINE_PLANES
    assert _SearchHit.model_fields["source"].default == "knowledge"


def test_corpus_stats_cache_is_keyed_by_embedding_model() -> None:
    """The count filters on the embedding model, so two models in one
    namespace each read their own N / avgdl instead of sharing one for 5 minutes."""
    from server.knowledge.app import bm25 as knowledge_bm25

    knowledge_bm25._CORPUS_CACHE.clear()
    try:
        ch = StubCH([{"namespace": "n", "n": 10, "avgdl": 4.0}])
        asyncio.run(knowledge_bm25.corpus_stats(ch, org="o", namespaces=["n"], embedding_model="a"))
        ch.rows = [{"namespace": "n", "n": 3, "avgdl": 9.0}]
        got = asyncio.run(
            knowledge_bm25.corpus_stats(ch, org="o", namespaces=["n"], embedding_model="b")
        )
        assert got == (3, 9.0) and [c[1]["model"] for c in ch.calls] == ["a", "b"]
    finally:
        knowledge_bm25._CORPUS_CACHE.clear()
