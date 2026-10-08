"""The graph-stats ranks leg compares fresh ranks with the file cards' rank
columns (`file_card_ranks`) and reads full rows, vector included, only for the
cards it rewrites (`live_file_cards`). Reading every card of the namespace in
full (`SELECT * FINAL`, ~6 KB a row) would run past the 30 s read deadline on a
large repository."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.workers import graph_stats as w


class _Repo:
    def __init__(self) -> None:
        self.full_reads: list[list[str]] = []
        self.inserted: list[dict[str, Any]] = []
        # a.cpp keeps its rank; b.cpp's rank moves; c.cpp has no card yet
        self.ranks = {"src/a.cpp": (0.5, 0, 1), "src/b.cpp": (0.1, 1, 0)}

    async def graph_stats_state(self, org: str, ns: str) -> dict[str, Any]:
        return {}

    async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
        return {"src/a.cpp": "h1", "src/b.cpp": "h2", "src/c.cpp": "h3"}

    async def include_edges(self, org: str, ns: str) -> list[tuple[str, str]]:
        return [("src/a.cpp", "src/b.cpp")]

    async def live_summary_rows(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("the pass must not read every card in full")

    async def file_card_ranks(self, org: str, ns: str, model: str) -> dict[str, Any]:
        return dict(self.ranks)

    async def live_file_cards(
        self, org: str, ns: str, model: str, paths: list[str]
    ) -> list[dict[str, Any]]:
        self.full_reads.append(list(paths))
        return [
            {
                "scope_path": p,
                "scope_type": "file",
                "summary": "card",
                "embedding": [0.1],
                "pagerank": 0.1,
            }
            for p in paths
        ]

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        if table == "code_summaries":
            self.inserted.extend(rows)

    def __getattr__(self, name: str) -> Any:  # every other leg: nothing to do
        async def empty(*a: Any, **k: Any) -> Any:
            return []

        return empty


def test_only_changed_cards_are_read_in_full_and_mirrored(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.domain.index import pagerank as pr
    from server.code.store import index as code_repo

    repo = _Repo()
    monkeypatch.setattr(code_repo, "CodeRepo", _repo_class(repo))
    monkeypatch.setattr(w, "_git_head", _async(""))
    monkeypatch.setattr(
        pr, "pagerank", lambda edges: {"src/a.cpp": 0.5, "src/b.cpp": 0.9, "src/c.cpp": 0.2}
    )
    monkeypatch.setattr(pr, "degrees", lambda edges: {"src/a.cpp": (0, 1), "src/b.cpp": (1, 0)})
    monkeypatch.setattr(
        "server.embedding.embedder.get_embedder", lambda: SimpleNamespace(model_name="m")
    )
    result = w.CodeGraphStatsResult()
    asyncio.run(w._run_repo("o", "n", result))
    assert repo.full_reads == [["src/b.cpp"]], "a.cpp is unchanged; c.cpp has no card to mirror"
    by_path = {r["scope_path"]: r for r in repo.inserted}
    assert set(by_path) == {"src/b.cpp", "src/c.cpp"}
    assert by_path["src/b.cpp"]["pagerank"] == 0.9 and by_path["src/b.cpp"]["embedding"] == [0.1]
    assert by_path["src/b.cpp"]["summary"] == "card", "the mirrored row keeps every column"
    assert by_path["src/c.cpp"]["pagerank"] == 0.2
    assert result.files_ranked == 2


def _repo_class(instance: _Repo) -> Any:
    class Cls:
        def __new__(cls, *a: Any, **k: Any) -> _Repo:
            return instance

        tombstone_of = staticmethod(lambda table, live: dict(live))

    return Cls


def _async(value: Any) -> Any:
    async def f(*a: Any, **k: Any) -> Any:
        return value

    return f
