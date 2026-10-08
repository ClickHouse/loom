"""In `best_chunk` mode the card leg reads source cards only
and brings each voted file's chunk nearest the query, ranked by distance;
`skeleton` mode keeps the skeleton-card shape."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.code.store.index import CodeRepo
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


def _row(cid: str, path: str, **kw: Any) -> dict[str, Any]:
    return {"chunk_id": cid, "path": path, **_UNIT, **kw}


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return []


def test_best_chunks_read_is_one_keyed_cosine_read_limited_per_path() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    asyncio.run(
        repo.best_chunks_for_paths(
            "o", "n", "m", [0.1, 0.2], ["b.cpp", "a.cpp", "a.cpp"], per_path=2
        )
    )
    sql, params = ch.calls[0]
    assert "cosineDistance(embedding, {q:Array(Float32)}) AS distance" in sql
    assert "path IN {paths:Array(String)}" in sql and "LIMIT {per_path:UInt32} BY path" in sql
    assert params["paths"] == ["a.cpp", "b.cpp"] and params["per_path"] == 2 and params["lim"] == 4
    assert asyncio.run(repo.best_chunks_for_paths("o", "n", "m", [0.1], [])) == []


class _Repo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [_row("L1", "src/lex.cpp", lex_score=40.0)]

    async def vector_search_chunks(self, *a, **k):
        return [_row("V1", "src/vec.cpp", distance=0.3)]

    async def vector_search_cards(
        self, org, ns, model, vec, *, path_prefix="", pool=15, skip_tests=False
    ):
        self.calls.append(("cards", {"pool": pool, "skip_tests": skip_tests}))
        return [{"path": "src/Common/MemoryTracker.cpp", "distance": 0.4}]

    async def best_chunks_for_paths(self, org, ns, model, vec, paths, *, per_path=1):
        self.calls.append(("best", {"paths": paths, "per_path": per_path}))
        return [_row("B1", "src/Common/MemoryTracker.cpp", distance=0.35)]

    async def chunks_for_paths(self, *a, **k):
        self.calls.append(("skeleton", k))
        return [_row("S1", "src/Common/MemoryTracker.cpp")]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]


def _run(mode: str):
    repo = _Repo()
    with patch.multiple(
        settings,
        code_fusion_signals=False,
        code_search_card_leg=True,
        code_search_card_leg_mode=mode,
        code_search_card_leg_files=10,
        code_search_card_leg_units_per_file=1,
        code_search_abstain_card_below=0.0,
        code_search_concept_expand_model="",
        code_search_card_leg_kinds="any",
    ):
        res = asyncio.run(
            cs.search(
                repo,
                "o",
                "n",
                query="where is the memory limit for a query enforced",
                top_k=5,
                embedder=_Embedder(),
            )
        )
    return res, repo


def test_best_chunk_mode_reads_source_cards_and_the_nearest_chunk() -> None:
    res, repo = _run("best_chunk")
    kinds = [c[0] for c in repo.calls]
    assert kinds == ["cards", "best"]
    assert repo.calls[0][1] == {"pool": 10, "skip_tests": True}
    assert repo.calls[1][1] == {"paths": ["src/Common/MemoryTracker.cpp"], "per_path": 1}
    unit = next(u for u in res.units if u.unit_id == "B1")
    assert unit.via == "card"


def test_skeleton_mode_is_unchanged() -> None:
    res, repo = _run("skeleton")
    kinds = [c[0] for c in repo.calls]
    assert kinds == ["cards", "skeleton"] and repo.calls[0][1]["skip_tests"] is False
    assert any(u.unit_id == "S1" for u in res.units)
