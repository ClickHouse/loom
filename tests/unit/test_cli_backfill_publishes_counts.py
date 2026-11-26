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
    def __init__(self, pages: int, counts_row: str | None = "{}") -> None:
        self.pages = pages
        self.counts_row = counts_row
        self.refreshed: list[tuple[str, str, str]] = []

    async def get_state_or_none(self, org: str, ns: str, key: str) -> str | None:
        return self.counts_row if key == "table_counts" else None

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
def quiet(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    declared: list[str] = []

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def declare(_ch: Any, _args: argparse.Namespace) -> None:
        declared.append("declared")

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Embedder())
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(code_backfill, "_record_manual_pass", nothing)
    monkeypatch.setattr(code_backfill, "_compact_after_bulk", nothing)
    monkeypatch.setattr(code_backfill, "connect_code_plane", declare)
    return declared


def test_a_phase_that_embedded_republishes_the_counts(quiet: list[str]) -> None:
    repo = _Repo(pages=2)
    rc = asyncio.run(code_backfill._backfill_embed(repo, "acme", "code-a", argparse.Namespace()))
    assert rc == 0
    assert repo.refreshed == [("table_counts", "acme", "code-a"), ("coverage", "acme", "code-a")]


def test_a_resumed_embed_phase_with_nothing_left_republishes(quiet: list[str]) -> None:
    # the last page landed and the process died before the phase end
    repo = _Repo(pages=0)
    rc = asyncio.run(code_backfill._backfill_embed(repo, "acme", "code-a", argparse.Namespace()))
    assert rc == 0
    assert quiet == ["declared"]
    assert repo.refreshed == [("table_counts", "acme", "code-a"), ("coverage", "acme", "code-a")]


def test_an_empty_embed_phase_for_an_unindexed_org_writes_no_counts(quiet: list[str]) -> None:
    # a mistyped --org: nothing pending and no counts row, so nothing is declared or written
    repo = _Repo(pages=0, counts_row=None)
    rc = asyncio.run(code_backfill._backfill_embed(repo, "acmee", "code-a", argparse.Namespace()))
    assert rc == 0
    assert quiet == []
    assert repo.refreshed == []


@pytest.mark.parametrize("files", [3, 0])
def test_the_index_phase_republishes_at_its_end(quiet: list[str], files: int) -> None:
    # files=0: a resumed index whose cursor was already at the end
    repo = _Repo(pages=0)
    counters = {"files": files, "chunks": files, "symbols": 0, "edges": 0, "errors": 0}
    asyncio.run(code_backfill._close_index(repo, counters, "acme", "code-a", "m", 0.0, "h", "full"))
    assert len(repo.refreshed) == 2


def test_a_failed_refresh_does_not_fail_the_phase(
    quiet: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    class _Down(_Repo):
        async def refresh_table_counts(self, org: str, ns: str) -> None:
            raise ConnectionError("down")

    repo = _Down(pages=1)
    rc = asyncio.run(code_backfill._backfill_embed(repo, "acme", "code-a", argparse.Namespace()))
    assert rc == 0
    assert repo.refreshed == [("coverage", "acme", "code-a")]
    assert "table counts not republished: ConnectionError: down" in capsys.readouterr().err


def test_a_full_index_run_publishes_coverage_at_its_new_watermark(
    quiet: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rows carry the counts fingerprint, `git_head` included, the way
    # `refresh_coverage` stamps them; a reader serves the row only while it matches.
    from server.code.domain.index.state import IndexState
    from server.code.store.index.status import counts_fingerprint, parse_shared_coverage

    class _StateRepo(_Repo):
        def __init__(self) -> None:
            super().__init__(pages=0)
            self.state: IndexState = {"git_head": {"value": "old"}}

        async def get_state(self, org: str, ns: str, key: str) -> str:
            return str(self.state.get(key, {}).get("value") or "")

        async def all_state(self, org: str, ns: str) -> IndexState:
            return {k: v.copy() for k, v in self.state.items()}

        async def set_state(self, org: str, ns: str, key: str, value: str, **k: Any) -> None:
            self.state[key] = {"value": value, "detail": str(k.get("detail", ""))}

        async def refresh_table_counts(self, org: str, ns: str) -> None:
            await self.set_state(org, ns, "table_counts", "{}", detail=await self._print(org, ns))

        async def refresh_coverage(self, org: str, ns: str) -> None:
            cov = '{"cov": {}, "gaps": null}'
            await self.set_state(org, ns, "coverage", cov, detail=await self._print(org, ns))

        async def _print(self, org: str, ns: str) -> str:
            return counts_fingerprint(await self.all_state(org, ns))

    async def head(*_a: Any) -> str:
        return "new"

    async def tree(*_a: Any) -> list[str]:
        return ["a.py"]

    async def written(*_a: Any, **_k: Any) -> str:
        return ""  # the batch landed, nothing held

    async def no_denylist(*_a: Any) -> None:
        return None

    repo = _StateRepo()
    cfg = argparse.Namespace(
        org="acme", namespace="code-a", git_dir="/g", git_branch="main", path_prefixes=("",)
    )
    cfg.embed_reuse_from = ""
    from server.code.store import index as code_index

    monkeypatch.setattr(code_index, "CodeRepo", lambda: repo)
    monkeypatch.setattr(code_backfill, "_repo_cfg", lambda args: cfg)
    monkeypatch.setattr("server.cli.code_backfill.os.path.isdir", lambda p: True)
    monkeypatch.setattr(code_backfill, "_backfill_head", head)
    monkeypatch.setattr(code_backfill, "_tree_paths_with_coverage", tree)
    monkeypatch.setattr(code_backfill, "_held_batch", written)
    monkeypatch.setattr("server.code.app.index.denylist.load_denylist", no_denylist)
    args = argparse.Namespace(
        org="acme",
        namespace="code-a",
        phase="index",
        embed=False,
        limit=0,
        paths_file="",
        concurrency=1,
        no_edges_second_pass=True,
    )
    asyncio.run(code_backfill._backfill(args))
    final = counts_fingerprint(repo.state)
    assert repo.state["git_head"]["value"] == "new"
    assert repo.state["table_counts"]["detail"] == final
    assert parse_shared_coverage(repo.state.get("coverage"), final) is not None
