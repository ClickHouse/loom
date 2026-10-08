"""`apply_delta` walks a delta in rounds: one
read per table for a round's paths, the per-file key-diff unchanged, one
insert per table per round. Only a real `CodeRepo` (or a stand-in that says
`supports_round_reads = True`) takes the round path; every other fake —
including an `AsyncMock`, which answers any attribute — keeps the per-file
path, so the existing per-file tests hold unchanged."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app.index import scip_delta as sd


class _Cfg:
    path_prefixes = ("src/",)


def _delta(n: int) -> sd.ScipDelta:
    files = []
    for i in range(n):
        files.append(
            sd.ScipFile(
                path=f"src/f{i}.cpp",
                symbols=[
                    sd.ScipSymbol(
                        qualified_name=f"ns::fn{i}",
                        name=f"fn{i}",
                        kind="function",
                        signature="()",
                        line_start=1,
                        line_end=5,
                        scip_symbol=f"s{i}",
                    )
                ],
                edges=[
                    sd.ScipEdge(
                        edge_kind="calls",
                        from_qualified_name=f"ns::fn{i}",
                        from_signature="()",
                        to_qualified_name="ns::sink",
                        to_signature="()",
                        line=3,
                        confidence=1.0,
                    )
                ],
            )
        )
    return sd.ScipDelta(
        delta_version=1, commit="c" * 40, files=files, deleted_files=["src/gone.cpp"]
    )


class _RoundRepo:
    supports_round_reads = True

    def __init__(self) -> None:
        self.reads: list[tuple[str, list[str]]] = []
        self.inserts: list[tuple[str, int]] = []
        self.state: dict[str, str] = {}

    async def live_symbols_for_paths(self, org, ns, paths, tier):
        assert tier in ("scip", "treesitter")  # The parser tier's spans too
        self.reads.append((f"symbols:{tier}", list(paths)))
        if tier == "treesitter":
            return {}
        # one path already holds its symbol unchanged → kept, not rewritten
        return {
            "src/f0.cpp": [
                {
                    "qualified_name": "ns::fn0",
                    "symbol_id": sd.symbol_id("ns::fn0", "()"),
                    "start_line": 1,
                    "end_line": 5,
                }
            ]
        }

    async def live_edges_for_paths(self, org, ns, paths, tier):
        self.reads.append(("edges", list(paths)))
        return {}

    async def chunk_spans_for_paths(self, org, ns, paths):
        self.reads.append(("spans", list(paths)))
        # f1's edge line sits inside a class skeleton and outside any function → dropped
        return {"src/f1.cpp": [{"kind": "class_skeleton", "start_line": 1, "end_line": 10}]}

    async def insert_rows(self, table, rows):
        self.inserts.append((table, len(rows)))

    async def set_state(self, org, ns, key, value, detail=""):
        self.state[key] = value


def test_a_delta_is_applied_in_rounds_with_one_read_and_one_insert_per_table() -> None:
    repo = _RoundRepo()
    with patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()):
        stats = asyncio.run(sd.apply_delta(repo, "o", "n", _delta(250)))  # type: ignore[arg-type]
    # 250 files + 1 deletion = 251 → three rounds of ≤100; three reads each
    rounds = [r for r in repo.reads if r[0] == "symbols:scip"]
    assert [len(r[1]) for r in rounds] == [100, 100, 51]
    assert len(repo.reads) == 12  # four reads a round (the parser tier's included)
    # one insert per table per round; the deletion's round still inserts
    assert [t for t, _ in repo.inserts] == ["code_symbols", "code_edges"] * 3
    assert stats.files == 251
    assert stats.symbols_new == 249  # f0 unchanged and unmoved → kept
    assert stats.edges_new == 249  # f1's edge was a declaration line
    assert stats.edges_dropped_declarations == 1
    assert sum(n for t, n in repo.inserts if t == "code_symbols") == 249
    assert sum(n for t, n in repo.inserts if t == "code_edges") == 249


class _PerFileRepo:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.inserts: list[tuple[str, int]] = []

    async def live_symbols_for_path(self, org, ns, path, tier):
        if tier == "scip":  # the parser-tier read rides the same per-file walk
            self.calls.append(path)
        return []

    async def live_edges_for_path(self, org, ns, path, tier):
        return []

    async def chunk_spans_for_path(self, org, ns, path):
        return []

    async def insert_rows(self, table, rows):
        self.inserts.append((table, len(rows)))

    async def set_state(self, *a: Any, **k: Any) -> None:
        return None


def test_a_repository_without_round_reads_keeps_the_per_file_path() -> None:
    from unittest.mock import AsyncMock

    from server.code.store.index import CodeRepo

    assert sd._has_round_reads(AsyncMock()) is False
    assert sd._has_round_reads(CodeRepo.__new__(CodeRepo)) is True
    repo = _PerFileRepo()
    with patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()):
        stats = asyncio.run(sd.apply_delta(repo, "o", "n", _delta(3)))  # type: ignore[arg-type]
    assert repo.calls == ["src/f0.cpp", "src/f1.cpp", "src/f2.cpp", "src/gone.cpp"]
    assert stats.files == 4 and stats.symbols_new == 3 and stats.edges_new == 3
    # per file: one insert per table, even when empty (the historical shape)
    assert len(repo.inserts) == 8
