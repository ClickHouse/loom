"""A second vector model on code.search: the vector leg
reads the request-named label, the lexical leg keeps the served one, the
shortlist arm rescores on the served vectors by key, inserts route a mapped
label to its own table, the placeholder pads to that table's width, and the
memo key tells the arms apart. Defaults unchanged: without `vector_model`
the leg calls are exactly the single-model ones."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import search as cs
from server.code.store.index import CodeRepo, build_row
from server.core.vectors import code_tables as vt
from server.embedding import embedder as emb
from server.settings import settings

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
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

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
        return [
            {"chunk_id": "c", "path": "src/c.cpp", "distance": 0.3},
            {"chunk_id": "d", "path": "src/d.cpp", "distance": 0.4},
        ]

    async def exact_rerank_by_ids(self, *a, **k):
        self.calls.append(("exact", a, k))
        return [_vec("d", 0.05), _vec("c", 0.2)]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    def __init__(self, name: str, dim: int) -> None:
        self.model_name, self.dim, self.calls = name, dim, 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [0.1] * self.dim


def _run(repo, embedder, **kw):
    import asyncio

    with patch.object(settings, "code_fusion_signals", False):
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


def test_default_path_is_unchanged() -> None:
    repo, e = _Repo(), _Embedder("text-embedding-3-large", 4)
    res = _run(repo, e)
    kinds = [c[0] for c in repo.calls]
    assert "vector" in kinds and "shortlist" not in kinds and "exact" not in kinds
    vec = next(c for c in repo.calls if c[0] == "vector")
    assert vec[1][2] == "text-embedding-3-large"
    assert "vector_model" not in res.score_explanation


def test_vector_model_reads_its_own_label_and_lexical_keeps_the_served_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emb.reset()
    second = _Embedder("static:tiny", 2)
    monkeypatch.setattr(emb, "get_secondary_embedder", lambda spec: second)
    repo, primary = _Repo(), _Embedder("text-embedding-3-large", 4)
    res = _run(repo, primary, vector_model="static:tiny", vector_shortlist=0)
    vec = next(c for c in repo.calls if c[0] == "vector")
    lex = next(c for c in repo.calls if c[0] == "lexical")
    assert vec[1][2] == "static:tiny" and len(vec[1][3]) == 2
    assert lex[1][2] == "text-embedding-3-large"
    assert second.calls == 1 and primary.calls == 0
    assert "vector_model=static:tiny" in res.score_explanation
    assert res.retrieval_work["embedding_calls"] == 1


def test_shortlist_then_exact_rescore_on_the_served_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = _Embedder("static:tiny", 2)
    monkeypatch.setattr(emb, "get_secondary_embedder", lambda spec: second)
    repo, primary = _Repo(), _Embedder("text-embedding-3-large", 4)
    res = _run(repo, primary, vector_model="static:tiny", vector_shortlist=1000)
    kinds = [c[0] for c in repo.calls]
    assert "shortlist" in kinds and "exact" in kinds and "vector" not in kinds
    short = next(c for c in repo.calls if c[0] == "shortlist")
    exact = next(c for c in repo.calls if c[0] == "exact")
    assert short[1][2] == "static:tiny" and short[2]["n"] == 1000
    assert exact[1][2] == "text-embedding-3-large" and len(exact[1][3]) == 4
    assert exact[1][4] == [("src/c.cpp", "c"), ("src/d.cpp", "d")]
    assert second.calls == 1 and primary.calls == 1
    assert res.retrieval_work["vector_shortlist"] == 2
    assert "shortlist=1000" in res.score_explanation
    # the exact stage's distances rank the pool: d (0.05) leads c
    paths = [u.path for u in res.units]
    assert paths.index("src/d.cpp") < paths.index("src/c.cpp")


def test_shortlist_setting_is_the_default_when_the_request_leaves_it_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = _Embedder("static:tiny", 2)
    monkeypatch.setattr(emb, "get_secondary_embedder", lambda spec: second)
    repo, primary = _Repo(), _Embedder("text-embedding-3-large", 4)
    with patch.object(settings, "code_search_vector_shortlist", 500):
        _run(repo, primary, vector_model="static:tiny")
    short = next(c for c in repo.calls if c[0] == "shortlist")
    assert short[2]["n"] == 500


# ---- the table map ----------------------------------------------------------


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, int]] = []
        self.query_all_calls: list[tuple[str, dict]] = []

    async def insert(self, table, rows, **k):
        self.inserts.append((table, len(rows)))

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return []


@pytest.fixture
def mapped(monkeypatch: pytest.MonkeyPatch):
    vt.reset()
    monkeypatch.setattr(
        vt,
        "_mapping",
        lambda: {"static:tiny": ("loom.scratch_tiny", 256)},
    )
    yield
    vt.reset()


def test_table_map_parses_validates_and_defaults() -> None:
    vt.reset()
    m = vt._parse('{"static:tiny": {"table": "loom.scratch_tiny", "dim": 256}, "x": "other_t"}')
    assert m == {"static:tiny": ("loom.scratch_tiny", 256), "x": ("loom.other_t", 0)}
    with pytest.raises(ValueError):
        vt._parse('{"bad": {"table": "loom.x; DROP TABLE y", "dim": 1}}')
    with pytest.raises(ValueError):
        vt._parse('["not", "an", "object"]')
    assert vt._parse("") == {}
    vt.reset()
    assert vt.code_vector_table("text-embedding-3-large") == "loom.code_chunks"


@pytest.mark.asyncio
async def test_inserts_route_a_mapped_label_to_its_table(mapped) -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    rows = [
        build_row(
            "code_chunks",
            org="o",
            namespace="n",
            embedding_model="text-embedding-3-large",
            path="a",
            chunk_id="1",
            content="x",
        ),
        build_row(
            "code_chunks",
            org="o",
            namespace="n",
            embedding_model="static:tiny",
            path="a",
            chunk_id="1",
            content="x",
        ),
        build_row(
            "code_chunks",
            org="o",
            namespace="n",
            embedding_model="static:tiny",
            path="b",
            chunk_id="2",
            content="y",
        ),
    ]
    await repo.insert_rows("code_chunks", rows)
    assert sorted(ch.inserts) == [("loom.code_chunks", 1), ("loom.scratch_tiny", 2)]
    await repo.insert_rows("code_symbols", [build_row("code_symbols", org="o", namespace="n")])
    assert ch.inserts[-1] == ("loom.code_symbols", 1)


@pytest.mark.asyncio
async def test_vector_reads_follow_the_map_and_the_exact_stage_reads_the_served_table(
    mapped,
) -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.vector_search_chunks("o", "n", "static:tiny", [0.1] * 256)
    await repo.vector_shortlist_ids("o", "n", "static:tiny", [0.1] * 256, n=1000)
    await repo.exact_rerank_by_ids(
        "o",
        "n",
        "text-embedding-3-large",
        [0.1] * 4,
        [("p", "c1"), ("p", "c1"), ("q", "c2")],
        pool=200,
    )
    await repo.vector_search_chunks("o", "n", "text-embedding-3-large", [0.1] * 4)
    sqls = [s for s, _ in ch.query_all_calls]
    assert "FROM loom.scratch_tiny FINAL" in sqls[0] and "FROM loom.scratch_tiny FINAL" in sqls[1]
    # The exact stage is two plain keyed reads on the served
    # table (vectors, then the keys whose newest version is a tombstone)
    assert "FROM loom.code_chunks\n" in sqls[2] and "FINAL" not in sqls[2]
    # Each key's newest version is ranked, never a stale copy
    assert "ORDER BY version DESC" in sqls[2] and "LIMIT 1 BY path, chunk_id" in sqls[2]
    assert sqls[2].index("LIMIT 1 BY path, chunk_id") < sqls[2].index("ORDER BY distance ASC")
    assert "argMax(tombstoned_at IS NULL, version) AS is_live" in sqls[3] and "FINAL" not in sqls[3]
    assert "FROM loom.code_chunks FINAL" in sqls[4]
    for s in sqls:
        assert "PREWHERE" not in s
    prm = ch.query_all_calls[2][1]
    assert (
        prm["paths"] == ["p", "q"]
        and prm["ids"] == ["c1", "c2"]
        and prm["model"] == "text-embedding-3-large"
    )
    assert "vector_search_index_fetch_multiplier" not in sqls[2]  # exact, no codes rescore
    assert await repo.exact_rerank_by_ids("o", "n", "m", [0.1], [], pool=5) == []


def test_placeholder_pads_a_mapped_table_to_its_own_width(mapped, monkeypatch) -> None:
    from server.core.vectors import placeholders as vp

    monkeypatch.setattr(vp, "PLACEHOLDER_COLUMNS", dict(vp.PLACEHOLDER_COLUMNS))
    from server.settings import settings

    with (
        patch.object(settings, "embedding_dim", 3072),
    ):
        rows = [{"embedding": [], "embedding_dim": 0}]
        assert vp.pad_rows("loom.scratch_tiny", rows) == 1
        assert len(rows[0]["embedding"]) == 256
        rows = [{"embedding": [], "embedding_dim": 0}]
        assert vp.pad_rows("loom.code_chunks", rows) == 1
        assert len(rows[0]["embedding"]) == 3072
        assert vp.pad_rows("loom.unrelated", [{"embedding": []}]) == 0


def test_a_placeholder_takes_its_coded_columns_width(monkeypatch) -> None:
    """Never scored, so it matches the column, not the served model: a write
    without a vector is never refused for its width."""
    from server.core.vectors import codes_state
    from server.core.vectors import placeholders as vp
    from server.settings import settings

    monkeypatch.setattr(codes_state, "_widths", {"loom.memory_embeddings.embedding": 3072})
    with patch.object(settings, "embedding_dim", 1024):
        rows = [{"embedding": [], "memory_key_embedding": []}]
        assert vp.pad_rows("memory_embeddings", rows) == 2
    assert len(rows[0]["embedding"]) == 3072
    assert len(rows[0]["memory_key_embedding"]) == 1024  # plain here: the setting
    asyncio.run(codes_state.refuse_other_widths("memory_embeddings", rows))


def test_memo_key_separates_the_arms() -> None:
    from server.code.api.models_search import CodeSearchInput
    from server.code.app import search_memo as memo

    base = dict(embedding_model="m", rerank="", token_budget=3000, recent_dirs=[])
    a = memo.key_for(CodeSearchInput(query="q"), **base)
    b = memo.key_for(CodeSearchInput(query="q", vector_model="static:tiny"), **base)
    c = memo.key_for(
        CodeSearchInput(query="q", vector_model="static:tiny", vector_shortlist=1000), **base
    )
    assert len({a, b, c}) == 3
