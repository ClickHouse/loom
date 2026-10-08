"""CodeRepo.table_counts is change-driven: seven FINAL scans per namespace
run only when a watermark moved or the cache aged out — not on every
code.repos poll (scanning on every poll costs on the order of 1 TB/day of reads)."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.store import index as cr
from server.code.store.index import status as code_status


class _CH:
    """Counts `count()` scans separately from the shared-row point read;
    `shared` is what the code_index_state row holds (None = no row)."""

    def __init__(self, shared: str | None = None) -> None:
        self.calls = 0
        self.point_reads = 0
        self.rows: dict[tuple[str, str], str] = {}
        self._seed = shared
        self.written: list[tuple[str, str, str]] = []

    @property
    def shared(self) -> str | None:
        return next(iter(self.rows.values()), None)

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if "code_index_state" in sql:
            self.point_reads += 1
            key = (params["org"], params["ns"])
            if key not in self.rows and self._seed is not None:
                self.rows[key] = self._seed
            v = self.rows.get(key)
            return {"state_value": v} if v is not None else None
        self.calls += 1
        return {"n": 7}


def _repo(shared: str | None = None) -> tuple[cr.CodeRepo, _CH]:
    ch = _CH(shared)
    repo = cr.CodeRepo.__new__(cr.CodeRepo)
    repo._ch = ch  # type: ignore[attr-defined]

    async def _set_state(org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        ch.written.append((key, value, detail))
        if key == "table_counts":
            ch.rows[(org, ns)] = value

    repo.set_state = _set_state  # type: ignore[method-assign]
    return repo, ch


def setup_function() -> None:
    code_status._COUNTS_CACHE.clear()


def test_same_fingerprint_serves_from_cache():
    repo, ch = _repo()
    a = asyncio.run(repo.table_counts("o", "ns", fingerprint="h1|s1|p1|c1"))
    b = asyncio.run(repo.table_counts("o", "ns", fingerprint="h1|s1|p1|c1"))
    # seven tables; code_chunks' embedded/embeddable ride its scan
    assert a == b and len(a) == 9
    assert {"code_chunks", "code_chunks_embedded", "code_chunks_embeddable"} <= a.keys()
    assert ch.calls == 7, "seven scans once, then the cache"


def test_moved_watermark_is_served_from_the_shared_row_and_ttl_re_reads_it(monkeypatch):
    """A reader never rescans while a shared row exists — a
    moved watermark or an aged cache re-reads the point; the ingest pass that
    moved the rows is what recomputes (refresh_table_counts)."""
    t = [1000.0]
    monkeypatch.setattr(code_status.time, "monotonic", lambda: t[0])
    repo, ch = _repo()
    asyncio.run(repo.table_counts("o", "ns", fingerprint="h1"))
    assert ch.calls == 7, "no row yet: the first reader scans once and publishes"
    asyncio.run(repo.table_counts("o", "ns", fingerprint="h2"))
    assert ch.calls == 7 and ch.point_reads == 2, (
        "a new git_head re-reads the point, not the tables"
    )
    asyncio.run(repo.table_counts("o", "ns", fingerprint="h2"))
    assert ch.point_reads == 2, "same fingerprint inside the ceiling: process cache"
    t[0] += 601
    asyncio.run(repo.table_counts("o", "ns", fingerprint="h2"))
    assert ch.calls == 7 and ch.point_reads == 3, (
        "past the ceiling: the point again, never the scan"
    )


def test_namespaces_are_cached_independently():
    repo, ch = _repo()
    asyncio.run(repo.table_counts("o", "a", fingerprint="x"))
    asyncio.run(repo.table_counts("o", "b", fingerprint="x"))
    asyncio.run(repo.table_counts("o", "a", fingerprint="x"))
    assert ch.calls == 14, (
        "each namespace's first reader scans once; the shared rows are per namespace"
    )


def test_state_fingerprint_uses_only_writer_watermarks():
    st = {
        "git_head": {"value": "abc"},
        "scip_commit": {"value": "def"},
        "pr_cursor": {"value": "2026-09-11"},
        "calibration": {"value": "noise"},
        "reconcile": {"value": "1284"},
    }
    fp = cr.state_fingerprint(st)
    assert fp == "abc|def||||2026-09-11"  # + scip_commit_ts, scip_commit_go
    st["reconcile"] = {"value": "0"}
    assert cr.state_fingerprint(st) == fp, "read-only state keys never bust the cache"


# --- the shared row -----------------------------------------------------------------


def test_shared_row_is_read_as_a_point_and_never_scanned():
    repo, ch = _repo(shared='{"code_files": 1, "code_edges": 2}')
    out = asyncio.run(repo.table_counts("o", "ns", fingerprint="h9"))
    assert out == {"code_files": 1, "code_edges": 2}
    assert ch.calls == 0 and ch.point_reads == 1, (
        "a row exists: no FINAL scan, whatever the fingerprint"
    )
    asyncio.run(repo.table_counts("o", "ns", fingerprint="h9"))
    assert ch.point_reads == 1, "then the process cache"


def test_missing_row_scans_once_and_publishes_it():
    repo, ch = _repo(shared=None)
    out = asyncio.run(repo.table_counts("o", "ns", fingerprint="h1"))
    # 6 table scans + the chunk/embedding row = 7 scans, 9 keys
    assert len(out) == 9 and ch.calls == 7
    assert ch.written and ch.written[0][0] == "table_counts" and ch.written[0][2] == "h1"
    # a second process (cold cache) now reads the point
    code_status._COUNTS_CACHE.clear()
    repo2 = cr.CodeRepo.__new__(cr.CodeRepo)
    repo2._ch = ch  # type: ignore[attr-defined]
    out2 = asyncio.run(repo2.table_counts("o", "ns", fingerprint="h2"))
    assert out2 == out and ch.calls == 7, (
        "the shared row serves the next process, even with a moved fingerprint"
    )


def test_refresh_publishes_the_counts_the_worker_computed():
    repo, ch = _repo(shared='{"code_files": 1}')
    out = asyncio.run(repo.refresh_table_counts("o", "ns", fingerprint="h3"))
    assert len(out) == 9 and ch.calls == 7
    assert ch.written[-1][0] == "table_counts" and ch.written[-1][2] == "h3"


def test_counts_fingerprint_leaves_the_pr_cursor_out():
    st = {
        "git_head": {"value": "abc"},
        "scip_commit": {"value": "def"},
        "pr_cursor": {"value": "2026-09-11"},
    }
    assert cr.counts_fingerprint(st) == "abc|def|||"
    st["pr_cursor"] = {"value": "2026-09-16"}
    assert cr.counts_fingerprint(st) == "abc|def|||", "the PR cursor moving never busts seven scans"
