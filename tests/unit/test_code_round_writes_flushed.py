"""Every round loop writes the rows its round buffered.

`RoundReads.insert_rows` buffers a round's writes until a read could see them or the round's owner flushes. A loop that drops
its `RoundReads` without flushing loses whatever the round wrote after its last
incidental flush — the last file's `code_files` row at least — so reconcile re-finds
those paths every pass and a bootstrap cursor moves past files it never wrote."""

from __future__ import annotations

import types
from typing import Any

import pytest

from server.cli import code_backfill
from server.code.app.index import bootstrap, reconcile
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
        repo, "o", "n", PATHS, source=_source, one=_write_one, denylist=None
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
