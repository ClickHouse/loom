"""`loom code backfill` walks each cursor batch in rounds of
`code_ingest_round_files`: the round's files are parsed once and their
live-row / name reads answered from round-wide statements, so the per-file
whole-namespace `live_symbols_for_path` scan (~2 files/s on a 50k-file
branch) is paid once per round."""

from __future__ import annotations

import asyncio

import pytest

from server.cli import code_backfill
from server.code.app.index import round_reads
from server.settings import settings


class _Repo:
    async def live_edges_for_paths(self, *a, **k):
        return {}

    async def live_symbols_for_paths(self, *a, **k):
        return {}

    async def live_call_misses_for_paths(self, *a, **k):
        return {}

    async def live_call_guards_for_paths(self, *a, **k):
        return {}

    async def name_candidates(self, *a, **k):
        return {}


def _drive(monkeypatch: pytest.MonkeyPatch, size: int, paths: list[str]) -> tuple[list, list]:
    monkeypatch.setattr(settings, "code_ingest_round_files", size, raising=False)
    prefetched: list[list[str]] = []
    seen: list[tuple[object, str, bytes]] = []
    real_prefetch = round_reads.RoundReads.prefetch

    async def _prefetch(self, sources, denylist=None):
        prefetched.append(sorted(sources))
        await real_prefetch(self, sources, denylist)

    monkeypatch.setattr(round_reads.RoundReads, "prefetch", _prefetch)

    async def _source(path: str) -> tuple[str, bytes | None]:
        if path.endswith(".missing"):
            return path, None
        return path, f"int {path.replace('/', '_').replace('.', '_')}() {{ return 1; }}\n".encode()

    async def _one(reads, path: str, source: bytes) -> None:
        seen.append((reads, path, source))

    asyncio.run(
        code_backfill._backfill_batch_rounds(
            _Repo(),
            "o",
            "ns",
            paths,
            source=_source,
            one=_one,
            denylist=None,
        )
    )
    return prefetched, seen


def test_a_batch_runs_in_rounds_with_one_prefetch_each(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [f"src/{i}.cpp" for i in range(5)] + ["src/gone.missing"]
    prefetched, seen = _drive(monkeypatch, 2, paths)
    # six paths in rounds of two → three prefetches; the missing source is
    # excluded from its round's prefetch and never ingested
    assert prefetched == [["src/0.cpp", "src/1.cpp"], ["src/2.cpp", "src/3.cpp"], ["src/4.cpp"]]
    assert [p for _, p, _ in seen] == [f"src/{i}.cpp" for i in range(5)]
    # every file is ingested against the round's wrapper, which holds its parse
    for reads, path, _ in seen:
        assert isinstance(reads, round_reads.RoundReads)
        assert path in reads.parsed
    rounds_seen = {id(r) for r, _, _ in seen}
    assert len(rounds_seen) == 3


def test_round_size_zero_keeps_the_per_file_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    prefetched, seen = _drive(monkeypatch, 0, ["a.cpp", "b.cpp"])
    assert prefetched == []
    assert [p for _, p, _ in seen] == ["a.cpp", "b.cpp"]
    # one wrapper for the whole batch; with nothing prefetched it delegates
    assert len({id(r) for r, _, _ in seen}) == 1
    assert all(r.parsed == {} for r, _, _ in seen)


def test_an_oversize_record_moves_the_counter_an_ingest_would() -> None:
    from server.code.app.index.ingest_stats import FileIngestStats

    counters = {"files": 0, "errors": 0}
    for recorded in (FileIngestStats(path="a.png", excluded="binary"), False):
        code_backfill._count_record(counters, recorded)
    code_backfill._count_record(counters, FileIngestStats(path="a.json", error="skipped: …"))
    assert counters == {"files": 1, "errors": 1, "excluded": 1}
