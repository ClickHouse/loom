"""Per-file saturation in code.search's top-k assembly —
the 2nd, 3rd... unit from one path decays by decay^excess after
diversification and before the cut; the file's best unit never moves."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.app.search.window import (
    _same_path_in_window,
    _saturate_files,
    _saturation_skip_reason,
)
from server.code.domain.units import CodeUnit
from server.settings import settings


def _u(path: str, score: float, line: int = 1, symbol: str = "") -> CodeUnit:
    return CodeUnit(
        unit_id=f"{path}:{line}", path=path, score=score, start_line=line, symbol=symbol
    )


def test_three_units_of_one_file_decay_geometrically_and_the_best_is_untouched() -> None:
    a1 = _u("src/A.cpp", 0.040, 10, "A::f")
    a2 = _u("src/A.cpp", 0.038, 80, "A::g")
    a3 = _u("src/A.cpp", 0.036, 200, "A::h")
    b = _u("src/B.cpp", 0.030, 5, "B::x")
    out, moved = _saturate_files([a1, a2, a3, b], 0.5, top_k=4)
    assert [u.unit_id for u in out] == [a1.unit_id, b.unit_id, a2.unit_id, a3.unit_id]
    assert out[0].score == 0.040, "the file's best unit keeps its (coherence-lifted) score"
    assert out[2].score == 0.019, "second unit of A: 0.038 * 0.5^1"
    assert out[3].score == 0.009, "third unit of A: 0.036 * 0.5^2"
    assert moved == 3, "slots 2-4 changed occupant; slot 1 never does"


def test_gentler_decay_moves_less() -> None:
    a1, a2, b = _u("src/A.cpp", 0.040), _u("src/A.cpp", 0.038, 2), _u("src/B.cpp", 0.020)
    out, moved = _saturate_files([a1, a2, b], 0.7, top_k=3)
    assert [u.path for u in out] == ["src/A.cpp", "src/A.cpp", "src/B.cpp"]
    assert out[1].score == round(0.038 * 0.7, 6) and moved == 0
    out5, moved5 = _saturate_files([a1, a2, b], 0.5, top_k=3)
    assert out5[1].path == "src/B.cpp" and moved5 == 2


def test_header_and_source_are_different_paths() -> None:
    h = _u("src/Storages/MergeTree/MergeTreeData.h", 0.040)
    cpp = _u("src/Storages/MergeTree/MergeTreeData.cpp", 0.039)
    out, moved = _saturate_files([h, cpp], 0.5, top_k=2)
    assert [u.score for u in out] == [0.040, 0.039] and moved == 0


def test_top_slot_never_moves_and_order_is_stable_on_ties() -> None:
    a1 = _u("src/A.cpp", 0.010, 1)
    a2 = _u("src/A.cpp", 0.010, 2)
    b = _u("src/B.cpp", 0.005)
    out, _ = _saturate_files([a1, a2, b], 0.5, top_k=3)
    assert out[0].unit_id == a1.unit_id
    assert [u.score for u in out] == [0.010, 0.005, 0.005]
    assert [u.unit_id for u in out[1:]] == [a2.unit_id, b.unit_id], "stable sort keeps fusion order"


def test_window_counter_and_skip_reasons() -> None:
    ranked = [
        _u("src/A.cpp", 0.04),
        _u("src/A.cpp", 0.03, 2),
        _u("src/B.cpp", 0.02),
        _u("src/A.cpp", 0.01, 3),
    ]
    assert _same_path_in_window(ranked, 3) == 1
    assert _same_path_in_window(ranked, 4) == 2
    assert _same_path_in_window(ranked, 1) == 0
    assert (
        _saturation_skip_reason(route="hybrid", path_prefix="src/", query="merge selection")
        == "path_prefix"
    )
    assert (
        _saturation_skip_reason(route="symbol_miss_hybrid", path_prefix="", query="MergeTreeData")
        == "symbol"
    )
    assert (
        _saturation_skip_reason(route="hybrid", path_prefix="", query="how are flaky tests retried")
        == "test_intent"
    )
    assert (
        _saturation_skip_reason(route="hybrid", path_prefix="", query="how are parts merged") == ""
    )


class _Repo:
    """Two legs that both put three chunks of one file ahead of one other file."""

    def __init__(self) -> None:
        self.rows = [
            {
                "chunk_id": "a1",
                "path": "src/A.cpp",
                "start_line": 1,
                "end_line": 9,
                "scope": "A::f",
                "lex_score": 9.0,
                "distance": 0.1,
            },
            {
                "chunk_id": "a2",
                "path": "src/A.cpp",
                "start_line": 20,
                "end_line": 29,
                "scope": "A::g",
                "lex_score": 8.0,
                "distance": 0.2,
            },
            {
                "chunk_id": "a3",
                "path": "src/A.cpp",
                "start_line": 40,
                "end_line": 49,
                "scope": "A::h",
                "lex_score": 7.0,
                "distance": 0.3,
            },
            {
                "chunk_id": "b1",
                "path": "src/B.cpp",
                "start_line": 1,
                "end_line": 9,
                "scope": "B::x",
                "lex_score": 6.0,
                "distance": 0.4,
            },
        ]

    async def code_corpus_stats(
        self, org: str, ns: str, tokens: list[str]
    ) -> tuple[dict[str, int], int]:
        return {}, 100, 56.0

    async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return list(self.rows)

    async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return list(self.rows)

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


def _run(**kw: Any) -> Any:
    from server.code.app.search import search

    return asyncio.run(
        search(_Repo(), "o", "n", query="how are parts merged", top_k=3, bodies="none", **kw)
    )


def test_search_off_counts_the_window_and_the_setting_decays(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "code_search_file_saturation", 0.0)
    monkeypatch.setattr(settings, "code_fusion_signals", False)  # bare RRF order
    off = _run()
    assert [u.path for u in off.units] == ["src/A.cpp"] * 3
    assert (
        off.retrieval_work["same_path_in_topk"] == 2 and "saturation" not in off.score_explanation
    )
    monkeypatch.setattr(settings, "code_search_file_saturation", 0.5)
    on = _run()
    assert [u.path for u in on.units] == ["src/A.cpp", "src/B.cpp", "src/A.cpp"]
    assert on.units[0].unit_id == off.units[0].unit_id, "top slot never moves"
    assert on.retrieval_work["saturation_moved"] == 2 and "saturation=0.5" in on.score_explanation
    # Three cards either way; the only byte difference is the swapped card's own
    # identifying text (B's span and symbol vs A's third window) — the decay
    # adds no field to a served unit.
    assert len(on.units) == len(off.units) == 3
    assert abs(on.tokens_returned - off.tokens_returned) <= 2, "saturation must not add bytes"


def test_setting_turns_it_on_and_path_prefix_skips(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "code_search_file_saturation", 0.7)
    monkeypatch.setattr(settings, "code_fusion_signals", False)
    on = _run()
    assert "saturation=0.7" in on.score_explanation
    scoped = _run(path_prefix="src/")
    assert "saturation=skipped(path_prefix)" in scoped.score_explanation
    assert [u.path for u in scoped.units] == ["src/A.cpp"] * 3
    monkeypatch.setattr(settings, "code_search_file_saturation", 0.0)
    assert "saturation" not in _run().score_explanation, "0 = off"
