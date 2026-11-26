"""A CLI backfill phase moves rows outside the ingest pass, which is what publishes
the shared counts and coverage rows a status read serves: each phase republishes
them at its end, a resumed phase with nothing left to write included."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pytest

from server.cli import code_backfill


class _Embedder:
    model_name, dim = "m", 2

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.5, 0.5] for _ in texts]


class _Repo:
    def __init__(self, pages: int) -> None:
        self.pages = pages
        self.refreshed: list[tuple[str, str, str]] = []

    async def pending_embed_chunks(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        if not self.pages:
            return []
        self.pages -= 1
        return [{"org": "acme", "namespace": "code-a", "content": "def f(): pass"}]

    def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
        return dict(live)

    async def insert_rows(self, table: str, rows: list[Any]) -> None:
        return None

    async def refresh_table_counts(self, org: str, ns: str) -> None:
        self.refreshed.append(("table_counts", org, ns))

    async def refresh_coverage(self, org: str, ns: str) -> None:
        self.refreshed.append(("coverage", org, ns))


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Embedder())
    monkeypatch.setattr(code_backfill, "_record_manual_pass", nothing)
    monkeypatch.setattr(code_backfill, "_compact_after_bulk", nothing)
    monkeypatch.setattr(code_backfill, "connect_code_plane", nothing)


def test_a_phase_that_embedded_republishes_the_counts(quiet: None) -> None:
    repo = _Repo(pages=2)
    rc = asyncio.run(code_backfill._backfill_embed(repo, "acme", "code-a", argparse.Namespace()))
    assert rc == 0
    assert repo.refreshed == [("table_counts", "acme", "code-a"), ("coverage", "acme", "code-a")]


def test_a_resumed_embed_phase_with_nothing_left_republishes(quiet: None) -> None:
    # the last page landed and the process died before the phase end
    repo = _Repo(pages=0)
    rc = asyncio.run(code_backfill._backfill_embed(repo, "acme", "code-a", argparse.Namespace()))
    assert rc == 0
    assert repo.refreshed == [("table_counts", "acme", "code-a"), ("coverage", "acme", "code-a")]


@pytest.mark.parametrize("files", [3, 0])
def test_the_index_phase_republishes_at_its_end(quiet: None, files: int) -> None:
    # files=0: a resumed index whose cursor was already at the end
    repo = _Repo(pages=0)
    counters = {"files": files, "chunks": files, "symbols": 0, "edges": 0, "errors": 0}
    asyncio.run(code_backfill._close_index(repo, counters, "acme", "code-a", "m", 0.0, "h", "full"))
    assert len(repo.refreshed) == 2


def test_a_failed_refresh_does_not_fail_the_phase(
    quiet: None, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Down(_Repo):
        async def refresh_table_counts(self, org: str, ns: str) -> None:
            raise ConnectionError("down")

    repo = _Down(pages=1)
    rc = asyncio.run(code_backfill._backfill_embed(repo, "acme", "code-a", argparse.Namespace()))
    assert rc == 0
    assert repo.refreshed == [("coverage", "acme", "code-a")]
    assert "table counts not republished: ConnectionError: down" in capsys.readouterr().err
