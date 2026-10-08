"""A file deleted at HEAD leaves no live row behind: tombstone_file retires
its chunks, parser symbols, parser edges, caller guards, declined call sites
and file row, each as a newer version of the same key, and forgets the
file's cached header text. The two disclosure tables are best effort: their
failure never keeps the file's own rows alive."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import ingest, ingest_headers, ingest_stats

PATH = "src/gone.cpp"


def _live(**key: Any) -> dict[str, Any]:
    return {"org": "o", "namespace": "n", "path": PATH, "version": 1, "tombstoned_at": None, **key}


class _Repo:
    def __init__(self, *, fail: str = "") -> None:
        self.fail = fail
        self.inserted: dict[str, list[dict[str, Any]]] = {}
        self.tiers: list[tuple[str, str]] = []

    async def live_chunks_for_path(
        self, org: str, ns: str, path: str, model: str, *, with_embedding: bool
    ) -> list[dict[str, Any]]:
        assert with_embedding, "a tombstone keeps the vector the live row had"
        return [_live(chunk_id="c1"), _live(chunk_id="c2")]

    async def live_symbols_for_path(
        self, org: str, ns: str, path: str, tier: str
    ) -> list[dict[str, Any]]:
        self.tiers.append(("symbols", tier))
        return [_live(symbol_id="s1", name="f", qualified_name="DB::f")]

    async def live_edges_for_path(
        self, org: str, ns: str, path: str, tier: str
    ) -> list[dict[str, Any]]:
        self.tiers.append(("edges", tier))
        return [_live(from_symbol_id="s1", to_symbol_id="s9")]

    async def live_call_guards_for_path(self, org: str, ns: str, path: str) -> list[Any]:
        if self.fail == "guards":
            raise RuntimeError("code_caller_guards unavailable")
        return [_live(from_symbol_id="s1")]

    async def live_call_misses_for_path(self, org: str, ns: str, path: str) -> list[Any]:
        if self.fail == "misses":
            raise RuntimeError("code_call_sites_unresolved unavailable")
        return [_live(callee_name="helper", line=7)]

    async def live_files_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        return [_live(lang="cpp")]

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.setdefault(table, []).extend(rows)


def _tombstone(repo: _Repo) -> ingest_stats.FileIngestStats:
    return asyncio.run(
        ingest.tombstone_file(repo, "o", "n", path=PATH, embedding_model="m")  # type: ignore[arg-type]
    )


def test_every_live_row_of_the_file_gets_a_tombstone_in_its_own_table() -> None:
    repo = _Repo()
    stats = _tombstone(repo)

    assert {t: len(rows) for t, rows in repo.inserted.items()} == {
        "code_chunks": 2,
        "code_symbols": 1,
        "code_edges": 1,
        "code_caller_guards": 1,
        "code_call_sites_unresolved": 1,
        "code_files": 1,
    }
    for rows in repo.inserted.values():
        # a newer version of the same key, marked retired
        assert all(r["tombstoned_at"] is not None and r["version"] > 1 for r in rows)
        assert all(r["path"] == PATH and r["namespace"] == "n" for r in rows)
    assert [r["chunk_id"] for r in repo.inserted["code_chunks"]] == ["c1", "c2"]
    assert [r["callee_name"] for r in repo.inserted["code_call_sites_unresolved"]] == ["helper"]
    # only the parser tier's rows: compiler-tier rows belong to their own pass
    assert repo.tiers == [("symbols", "treesitter"), ("edges", "treesitter")]
    assert (stats.chunks_tombstoned, stats.symbols_tombstoned, stats.edges_tombstoned) == (2, 1, 1)
    assert (stats.guards_tombstoned, stats.misses_tombstoned) == (1, 1)
    assert stats.symbol_names_removed == ["f"] and stats.symbols_tombstoned_ids == ["s1"]


@pytest.mark.parametrize("fail", ["guards", "misses"])
def test_a_failing_disclosure_table_still_retires_the_file(fail: str) -> None:
    repo = _Repo(fail=fail)
    stats = _tombstone(repo)
    table = "code_caller_guards" if fail == "guards" else "code_call_sites_unresolved"
    assert table not in repo.inserted
    assert len(repo.inserted["code_files"]) == 1
    assert len(repo.inserted["code_chunks"]) == 2
    assert (stats.guards_tombstoned if fail == "guards" else stats.misses_tombstoned) == 0


def test_the_cached_header_text_of_a_deleted_file_is_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest_headers, "_header_cache", {("o", "n", PATH): "struct Gone {};"})
    _tombstone(_Repo())
    assert ("o", "n", PATH) not in ingest_headers._header_cache
