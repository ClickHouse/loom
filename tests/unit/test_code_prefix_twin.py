"""A Matryoshka-prefix twin of the chunk vectors is the
vector leg's default shortlist. The label parses, the projection is a unit
prefix, the prefix embedder wraps the served one, a chunk insert fans a
projected copy out to the twin table, the fill statement is well-formed, and
the read path uses the default with one provider call and falls back to the
full scan when the twin has nothing."""

from __future__ import annotations

import asyncio
import math
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import search as cs
from server.code.domain.index import prefix_twin as pt
from server.code.store.index import CodeRepo
from server.code.store.index import prefix_twin as pt_store
from server.core.vectors import code_tables as vt
from server.core.vectors import prefix
from server.embedding import embedder as emb
from server.embedding.prefix import PrefixEmbedder
from server.settings import settings

_MAP = '{"prefix:2:text-embedding-3-large": {"table": "loom.code_chunks_prefix", "dim": 2}}'


@pytest.fixture
def twin_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_vector_tables", _MAP)
    monkeypatch.setattr(settings, "code_search_vector_models", "prefix:2:text-embedding-3-large")
    vt.reset()
    yield
    vt.reset()


def test_prefix_label_parses_and_projection_is_a_unit_prefix() -> None:
    assert prefix.parse_prefix_label("prefix:256:text-embedding-3-large") == (
        256,
        "text-embedding-3-large",
    )
    assert prefix.parse_prefix_label("static:potion-code-16M-v2") is None
    assert prefix.parse_prefix_label("prefix:0:x") is None
    v = prefix.project([3.0, 4.0, 100.0, 100.0], 2)
    assert v == [0.6, 0.8] and math.isclose(sum(x * x for x in v), 1.0)
    assert prefix.project([], 2) == []
    with pytest.raises(ValueError):
        prefix.project([1.0], 2)


class _Served:
    model_name, dim = "text-embedding-3-large", 4

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [3.0, 4.0, 0.0, 0.0]


def test_prefix_embedder_wraps_the_served_model_and_refuses_another() -> None:
    served = _Served()
    pe = PrefixEmbedder(base=served, spec="prefix:2:text-embedding-3-large")
    assert pe.model_name == "prefix:2:text-embedding-3-large" and pe.dim == 2
    assert asyncio.run(pe.embed("q")) == [0.6, 0.8] and served.calls == 1
    with pytest.raises(ValueError):
        PrefixEmbedder(base=served, spec="prefix:2:text-embedding-3-small")
    with pytest.raises(ValueError):
        PrefixEmbedder(base=served, spec="prefix:8:text-embedding-3-large")


def test_build_resolves_a_prefix_spec_against_the_served_embedder(
    monkeypatch: pytest.MonkeyPatch,
    twin_configured: None,
) -> None:
    served = _Served()
    monkeypatch.setattr(emb, "get_embedder", lambda: served)
    monkeypatch.setattr(emb, "_secondary", {})
    got = emb.get_secondary_embedder("prefix:2:text-embedding-3-large")
    assert isinstance(got, PrefixEmbedder) and got.base is served
    assert emb.provider_for("prefix:2:text-embedding-3-large") == "prefix"


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, rows))


def _chunk(cid: str, vec: list[float], *, tomb: Any = None) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "embedding_model": "text-embedding-3-large",
        "path": "src/a.cpp",
        "chunk_id": cid,
        "scope": "f",
        "kind": "function",
        "start_line": 1,
        "end_line": 2,
        "content": "x",
        "content_hash": "",
        "context_text": "",
        "context_description": "",
        "context_source": "structural",
        "lexemes": "",
        "symbol_ids": [],
        "embedding": vec,
        "embedding_dim": len(vec),
        "embed_attempts": 0,
        "embed_last_error": "",
        "embed_failed_at": None,
        "indexed_commit": "",
        "tombstoned_at": tomb,
        "created_at": 0,
        "version": 7,
    }


def test_chunk_insert_fans_a_projected_copy_out_to_the_twin(twin_configured: None) -> None:
    ch = _CH()
    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = ch
    rows = [_chunk("c1", [3.0, 4.0, 1.0, 1.0]), _chunk("c2", [], tomb=1)]
    asyncio.run(CodeRepo.insert_rows(repo, "code_chunks", rows))
    tables = [t for t, _ in ch.inserts]
    assert tables == ["loom.code_chunks", "loom.code_chunks_prefix"]
    twin = ch.inserts[1][1]
    assert [r["chunk_id"] for r in twin] == ["c1", "c2"]
    assert twin[0]["embedding"] == [0.6, 0.8] and twin[0]["embedding_dim"] == 2
    assert twin[0]["embedding_model"] == "prefix:2:text-embedding-3-large"
    assert twin[0]["version"] == 7 and "content" not in twin[0]
    # the tombstone follows as a placeholder version
    assert (
        twin[1]["embedding"] == []
        and twin[1]["embedding_dim"] == 0
        and twin[1]["tombstoned_at"] == 1
    )
    # the served rows themselves are untouched
    assert ch.inserts[0][1][0]["embedding"] == [3.0, 4.0, 1.0, 1.0]


def test_without_a_configured_twin_the_insert_is_what_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_vector_tables", "")
    vt.reset()
    ch = _CH()
    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = ch
    asyncio.run(CodeRepo.insert_rows(repo, "code_chunks", [_chunk("c1", [1.0, 0.0, 0.0, 0.0])]))
    assert [t for t, _ in ch.inserts] == ["loom.code_chunks"]


def test_fill_statement_has_matching_arity_and_no_column_aliases() -> None:
    sql = pt_store.fill_sql("loom.code_chunks_prefix", 256)
    cols = sql.split("(", 1)[1].split(")", 1)[0].split(",")
    select = sql.split("\nSELECT ", 1)[1].split("\nFROM", 1)[0]
    depth, n = 0, 1
    for ch in select:
        depth += ch == "("
        depth -= ch == ")"
        n += ch == "," and depth == 0
    assert n == len(cols) == len(pt.TWIN_COLUMNS)
    for c in pt.TWIN_COLUMNS:
        assert f" AS {c}" not in sql
    assert "FROM loom.code_chunks FINAL" in sql and "NOT IN" in sql
    assert (
        "L2Norm(head)" in sql
        and "arraySlice(CAST(embedding AS Array(Float32)), 1, {dim:UInt16})" in sql
    )


# ---- read path --------------------------------------------------------------

_UNIT = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="x",
    symbol_ids=[],
    context_description="",
)


def _lex(cid: str, score: float) -> dict[str, Any]:
    return {"chunk_id": cid, "lex_score": score, "path": f"src/{cid}.cpp", **_UNIT}


def _vec(cid: str, distance: float) -> dict[str, Any]:
    return {"chunk_id": cid, "distance": distance, "path": f"src/{cid}.cpp", **_UNIT}


class _Repo:
    def __init__(self, short_rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.short_rows = (
            short_rows
            if short_rows is not None
            else [
                {"chunk_id": "c", "path": "src/c.cpp", "distance": 0.3},
                {"chunk_id": "d", "path": "src/d.cpp", "distance": 0.4},
            ]
        )

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        self.calls.append(("lexical", a, k))
        return [_lex("a", 40.0), _lex("b", 10.0)]

    async def vector_search_chunks(self, *a, **k):
        self.calls.append(("vector", a, k))
        return [_vec("c", 0.1), _vec("a", 0.2)]

    async def vector_shortlist_ids(self, *a, **k):
        self.calls.append(("shortlist", a, k))
        return self.short_rows

    async def exact_rerank_by_ids(self, *a, **k):
        self.calls.append(("exact", a, k))
        return [_vec("d", 0.05), _vec("c", 0.2)]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


def _run(repo, embedder, **kw):
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_search_card_leg", False),
    ):
        return asyncio.run(
            cs.search(
                repo,
                "o",
                "n",
                query="how are parts merged on disk",
                top_k=5,
                embedder=embedder,
                **kw,
            )
        )


def _default_on(monkeypatch: pytest.MonkeyPatch, served: Any) -> None:
    monkeypatch.setattr(
        settings, "code_search_vector_model_default", "prefix:2:text-embedding-3-large"
    )
    monkeypatch.setattr(settings, "code_search_vector_shortlist", 200)
    monkeypatch.setattr(emb, "get_embedder", lambda: served)
    monkeypatch.setattr(emb, "_secondary", {})


def test_served_default_shortlists_on_the_twin_with_one_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    twin_configured: None,
) -> None:
    served, repo = _Served(), _Repo()
    _default_on(monkeypatch, served)
    res = _run(repo, served)
    kinds = [c[0] for c in repo.calls]
    assert "shortlist" in kinds and "exact" in kinds and "vector" not in kinds
    short = next(c for c in repo.calls if c[0] == "shortlist")
    exact = next(c for c in repo.calls if c[0] == "exact")
    assert short[1][2] == "prefix:2:text-embedding-3-large" and short[1][3] == [0.6, 0.8]
    assert short[2]["n"] == 200
    assert exact[1][2] == "text-embedding-3-large" and exact[1][3] == [3.0, 4.0, 0.0, 0.0]
    # one provider round trip serves both spaces
    assert served.calls == 1 and res.retrieval_work["embedding_calls"] == 1
    assert res.retrieval_work["vector_shortlist"] == 2
    assert "vector_model=prefix:2:text-embedding-3-large, shortlist=200" in res.score_explanation


def test_an_empty_shortlist_falls_back_to_the_full_scan(
    monkeypatch: pytest.MonkeyPatch,
    twin_configured: None,
) -> None:
    served, repo = _Served(), _Repo(short_rows=[])
    _default_on(monkeypatch, served)
    res = _run(repo, served)
    kinds = [c[0] for c in repo.calls]
    assert "shortlist" in kinds and "vector" in kinds and "exact" not in kinds
    vec = next(c for c in repo.calls if c[0] == "vector")
    assert vec[1][2] == "text-embedding-3-large" and vec[1][3] == [3.0, 4.0, 0.0, 0.0]
    assert res.retrieval_work["vector_shortlist_fallback"] == 1 and served.calls == 1


def test_default_is_off_without_a_shortlist_and_when_misconfigured(
    monkeypatch: pytest.MonkeyPatch,
    twin_configured: None,
) -> None:
    served = _Served()
    _default_on(monkeypatch, served)
    monkeypatch.setattr(settings, "code_search_vector_shortlist", 0)
    repo = _Repo()
    _run(repo, served)
    assert [c[0] for c in repo.calls if c[0] in ("shortlist", "exact", "vector")] == ["vector"]
    # a default that is not allow-listed degrades to the plain leg
    monkeypatch.setattr(settings, "code_search_vector_shortlist", 200)
    monkeypatch.setattr(settings, "code_search_vector_models", "")
    repo = _Repo()
    res = _run(repo, served)
    assert [c[0] for c in repo.calls if c[0] in ("shortlist", "exact", "vector")] == ["vector"]
    assert "vector_model" not in res.score_explanation


def test_a_request_vector_model_still_wins_over_the_default(
    monkeypatch: pytest.MonkeyPatch,
    twin_configured: None,
) -> None:
    served = _Served()
    _default_on(monkeypatch, served)
    with pytest.raises(ValueError):
        _run(_Repo(), served, vector_model="static:not-listed")
