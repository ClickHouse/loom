"""Every round loop writes the rows its round buffered.

`RoundReads.insert_rows` buffers a round's writes until a read could see them or the round's owner flushes. A loop that drops
its `RoundReads` without flushing loses whatever the round wrote after its last
incidental flush — the last file's `code_files` row at least — so reconcile re-finds
those paths every pass and a bootstrap cursor moves past files it never wrote."""

from __future__ import annotations

import argparse
import asyncio
import types
from typing import Any

import pytest

from server.cli import code_backfill
from server.code.app.index import bootstrap, gitrepo, ingest, reconcile
from server.code.app.index.ingest_stats import FileIngestStats
from server.llm import customer
from server.settings import settings
from tests.unit.code_fixtures import repo_config


class _Repo:
    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.written += [(table, str(r["path"])) for r in rows]

    async def live_edges_for_paths(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    async def live_symbols_for_paths(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    async def live_call_misses_for_paths(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    async def live_call_guards_for_paths(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    async def name_candidates(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


class _TimesOutOnce(_Repo):
    """The first `code_edges` INSERT runs past the write limit."""

    timed_out = False

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        if table == "code_edges" and not self.timed_out:
            self.timed_out = True
            raise TimeoutError("ClickHouse write statement exceeded 60 s")
        await super().insert_rows(table, rows)


PATHS = ["a.py", "b.py", "c.py"]
WANT = sorted(("code_files", p) for p in PATHS)


@pytest.fixture(autouse=True)
def _buffered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_ingest_round_files", 2)


async def _write_one(reads: Any, path: str, source: bytes) -> None:
    await reads.insert_rows("code_files", [{"path": path}])


async def _source(path: str) -> tuple[str, bytes]:
    return path, b"x = 1\n"


@pytest.mark.asyncio
async def test_reconcile_heal_writes_every_round() -> None:
    async def ingest_file(reads: Any, org: str, ns: str, *, path: str, **kw: Any) -> Any:
        await reads.insert_rows("code_files", [{"path": path}])
        return types.SimpleNamespace(chunks_new=0, chunks_moved=0, symbols_moved=0)

    async def show_file(git_dir: str, head: str, path: str, max_bytes: int = 0) -> bytes:
        return b"x = 1\n"

    repo = _Repo()
    cfg = repo_config(org="o", namespace="n", git_dir="/x")
    out = await reconcile.heal(
        repo,
        cfg,
        "h" * 40,
        PATHS,
        embedder=types.SimpleNamespace(model_name="m"),
        ingest_file=ingest_file,
        show_file=show_file,
    )
    assert out.files == 3 and out.rounds == 2
    assert sorted(repo.written) == WANT


@pytest.mark.asyncio
async def test_bootstrap_slice_rounds_write_every_round() -> None:
    repo = _Repo()
    attempted, partial = await bootstrap._slice_rounds(
        repo, "o", "n", PATHS, source=_source, one=_write_one, denylist=None, stop_after=None
    )
    assert (attempted, partial) == (3, False)
    assert sorted(repo.written) == WANT


@pytest.mark.asyncio
async def test_cli_backfill_rounds_write_every_round() -> None:
    repo = _Repo()
    await code_backfill._backfill_batch_rounds(
        repo,
        "o",
        "n",
        PATHS,
        source=_source,
        one=_write_one,
        denylist=None,
        git_dir="repo.git",
        head="a" * 40,
        counters={},
    )
    assert sorted(repo.written) == WANT


@pytest.mark.asyncio
async def test_a_failed_round_still_writes_what_it_buffered() -> None:
    async def one(reads: Any, path: str, source: bytes) -> None:
        await reads.insert_rows("code_files", [{"path": path}])
        if path == "b.py":
            raise RuntimeError("boom")

    repo = _Repo()
    with pytest.raises(RuntimeError):
        await bootstrap._slice_rounds(
            repo, "o", "n", PATHS, source=_source, one=one, denylist=None, stop_after=None
        )
    assert sorted(repo.written) == [("code_files", "a.py"), ("code_files", "b.py")]


@pytest.mark.asyncio
async def test_a_failed_write_is_counted_with_the_tables_behind_it_and_ends_the_batch() -> None:
    """The write fails when b.py's stage needs the buffer written, and that stage logs
    the failure: no file fails, and the rows of both files are gone. c.py is in the
    next round, which never starts."""

    async def one(reads: Any, path: str, source: bytes) -> None:
        await reads.insert_rows("code_symbols", [{"path": path}])
        await reads.insert_rows("code_edges", [{"path": path}, {"path": path}])
        await reads.insert_rows("code_files", [{"path": path}])
        if path == "b.py":
            with pytest.raises(TimeoutError):
                await reads.flush()

    repo = _TimesOutOnce()
    counters: dict[str, int] = {}
    await code_backfill._backfill_batch_rounds(
        repo,
        "o",
        "n",
        PATHS,
        source=_source,
        one=one,
        denylist=None,
        git_dir="repo.git",
        head="a" * 40,
        counters=counters,
    )
    assert code_backfill.unwritten(counters) == {"code_edges": 4, "code_files": 2}
    assert sorted(repo.written) == [("code_symbols", "a.py"), ("code_symbols", "b.py")]


@pytest.mark.asyncio
async def test_a_held_batch_keeps_the_rows_a_failed_write_left_out() -> None:
    """a.py's write fails inside a stage that logs it, then b.py's model call is held:
    the batch is held, and its counters still say what the write dropped."""

    async def one(reads: Any, path: str, source: bytes) -> None:
        await reads.insert_rows("code_edges", [{"path": path}])
        if path == "b.py":
            raise customer.ModelCallsDisabled("o's model calls are switched off")
        with pytest.raises(TimeoutError):
            await reads.flush()

    repo = _TimesOutOnce()
    counters: dict[str, int] = {}
    held = await code_backfill._held_batch(
        repo,
        "o",
        "n",
        PATHS,
        source=_source,
        one=one,
        denylist=None,
        git_dir="repo.git",
        head="a" * 40,
        counters=counters,
    )
    assert "switched off" in held
    assert code_backfill.unwritten(counters) == {"code_edges": 1}
    assert repo.written == [("code_edges", "b.py")]  # what the held round buffered


@pytest.mark.asyncio
async def test_a_failed_write_stops_either_pass_with_its_rows_counted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command's own batches. In the index phase the write fails in b.py's ingest,
    so b.py is one error; in the edge pass it fails as the round ends, where no file
    fails. Each pass's counters hold the rows of both files of the round, the batch
    says why the run stops, and c.py, in the next round or batch, is never read."""

    async def show_file(git_dir: str, head: str, path: str, max_bytes: int = 0) -> bytes:
        return b"x = 1\n"

    async def ingest_file(
        reads: Any, org: str, ns: str, *, path: str, edges_only: bool = False, **kw: Any
    ) -> FileIngestStats:
        await reads.insert_rows("code_edges", [{"path": path}, {"path": path}])
        if not edges_only:
            await reads.insert_rows("code_files", [{"path": path}])
            if path == "b.py":
                await reads.flush()  # as a read that could see the buffered rows does
        return FileIngestStats(path=path)

    monkeypatch.setattr(gitrepo, "show_file", show_file)
    monkeypatch.setattr(ingest, "ingest_file", ingest_file)
    repo = _TimesOutOnce()
    run = code_backfill._Run(
        repo=repo,
        cfg=types.SimpleNamespace(git_dir="repo.git", embed_reuse_from=""),
        args=argparse.Namespace(),
        org="o",
        ns="n",
        head="a" * 40,
        model_name="m",
        embedder=None,
        denylist=None,
        sem=asyncio.Semaphore(1),
    )
    assert await run.batch(PATHS) == "a write failed"
    assert run.counters == {
        "files": 1,
        "chunks": 0,
        "symbols": 0,
        "edges": 0,
        "errors": 1,
        "moved": 0,
        "embed_reused": 0,
        "unwritten:code_edges": 4,
        "unwritten:code_files": 2,
    }
    assert repo.written == []
    assert capsys.readouterr().err == (
        "  ! b.py: TimeoutError: ClickHouse write statement exceeded 60 s\n"
    )

    repo.timed_out = False
    monkeypatch.setattr(code_backfill, "_cursor_batches", lambda p: [(0, p[:2]), (2, p[2:])])
    second = await code_backfill._second_edge_pass(run, PATHS, None)
    assert second == {"files": 2, "edges": 0, "errors": 0, "unwritten:code_edges": 4}
    assert repo.written == []
    said = capsys.readouterr()
    assert said.err == (
        "  ! write as the round of a.py ended: "
        "TimeoutError: ClickHouse write statement exceeded 60 s\n"
    )
    assert "second edge pass stopped: a write failed: files=2 edges=+0 errors=0" in said.out
