"""A round's prefetch answers each file's chunk index and facet reads, so a heal
round sends one statement per table instead of one per file per table."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.app.index.round_reads import RoundReads

ORG, NS, MODEL = "o", "n", "m"


class _Repo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def _by_path(self, method: str, paths: list[str]) -> dict[str, list[dict[str, Any]]]:
        self.calls.append(method)
        return {p: [{"path": p, "from": method}] for p in paths}

    async def live_edges_for_paths(self, org: str, ns: str, paths: list[str], tier: str) -> Any:
        return await self._by_path("live_edges_for_paths", paths)

    async def live_symbols_for_paths(self, org: str, ns: str, paths: list[str], tier: str) -> Any:
        return await self._by_path("live_symbols_for_paths", paths)

    async def live_call_misses_for_paths(self, org: str, ns: str, paths: list[str]) -> Any:
        return await self._by_path("live_call_misses_for_paths", paths)

    async def live_call_guards_for_paths(self, org: str, ns: str, paths: list[str]) -> Any:
        return await self._by_path("live_call_guards_for_paths", paths)

    async def live_facets_for_paths(self, org: str, ns: str, paths: list[str]) -> Any:
        return await self._by_path("live_facets_for_paths", paths)

    async def live_chunk_index_for_paths(
        self, org: str, ns: str, paths: list[str], model: str
    ) -> Any:
        assert model == MODEL
        return await self._by_path("live_chunk_index_for_paths", paths)

    async def name_candidates(self, org: str, ns: str, names: list[str]) -> Any:
        self.calls.append("name_candidates")
        return {}

    async def live_facets_for_path(self, org: str, ns: str, path: str) -> Any:
        self.calls.append("live_facets_for_path")
        return []

    async def live_chunk_index_for_path(self, org: str, ns: str, path: str, model: str) -> Any:
        self.calls.append("live_chunk_index_for_path")
        return []


def _round(repo: _Repo, *, model: str) -> tuple[RoundReads, list[Any]]:
    sources = {"a.py": b"def f():\n    return g()\n", "b.py": b"X = 1\n"}

    async def go() -> list[Any]:
        reads = RoundReads(repo, ORG, NS)
        await reads.prefetch(sources, embedding_model=model)
        return [
            await reads.live_chunk_index_for_path(ORG, NS, "a.py", MODEL),
            await reads.live_facets_for_path(ORG, NS, "b.py"),
        ], reads

    answers, reads = asyncio.run(go())
    return reads, answers


def test_the_prefetch_answers_chunk_index_and_facets_without_a_per_file_read() -> None:
    repo = _Repo()
    reads, (index, facets) = _round(repo, model=MODEL)
    assert index == [{"path": "a.py", "from": "live_chunk_index_for_paths"}]
    assert facets == [{"path": "b.py", "from": "live_facets_for_paths"}]
    assert "live_chunk_index_for_path" not in repo.calls
    assert "live_facets_for_path" not in repo.calls
    assert repo.calls.count("live_chunk_index_for_paths") == 1


def test_without_a_model_the_chunk_index_is_read_per_file() -> None:
    repo = _Repo()
    _round(repo, model="")
    assert "live_chunk_index_for_paths" not in repo.calls
    assert "live_chunk_index_for_path" in repo.calls


def test_the_rechunk_heal_runs_at_the_configured_concurrency(monkeypatch: Any) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from server.code.app.index import ingest_pass as w
    from server.settings import settings

    monkeypatch.setattr(settings, "code_extractor_rechunk_concurrency", 16)
    seen: dict[str, Any] = {}

    async def fake_heal(*_a: Any, concurrency: int = 0, **_kw: Any) -> Any:
        seen["concurrency"] = concurrency
        return SimpleNamespace(files=1, identical=1, attempted=1)

    cfg = SimpleNamespace(org=ORG, namespace=NS, label="o/n")
    with (
        patch("server.code.app.index.reconcile.heal", new=fake_heal),
        patch("server.embedding.embedder.get_embedder", return_value=SimpleNamespace()),
    ):
        asyncio.run(
            w._rechunk_batch(
                object(),
                cfg,
                "h",
                ["a.py"],
                frozenset({"chunk"}),
                {"a.py": "x"},
                w.CodeIngestResult(),
                None,
                cursor=0,
                total=1,
            )
        )
    assert seen["concurrency"] == 16
