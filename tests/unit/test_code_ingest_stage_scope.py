"""A heal scoped to the extractor stages that moved runs only
those blocks of `ingest_file` — and only when the file's content is proven
unchanged — so a chunker fix does not re-read every file's symbols and edges
for rows the key-diff would keep."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from server.code.app.index import ingest, ingest_stats
from server.code.app.index.ingest import heal_scope

_SRC = b"int f() { return 1; }\nint g() { return f(); }\n"
_HASH = hashlib.sha256(_SRC).hexdigest()


class _Repo:
    """Records every read and write; `live` seeds what the reads return."""

    def __init__(self, live: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.live = live or {}
        self.reads: list[str] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def live_chunk_index_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_chunks")
        return self.live.get("code_chunks", [])

    async def chunks_full_by_ids(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_chunks:full")
        return []

    async def live_symbols_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_symbols")
        return self.live.get("code_symbols", [])

    async def name_candidates(self, *_: Any) -> dict[str, Any]:
        self.reads.append("code_symbol_names")
        return {}

    async def live_edges_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_edges")
        return self.live.get("code_edges", [])

    async def live_call_misses_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_call_sites_unresolved")
        return self.live.get("code_call_sites_unresolved", [])

    async def live_call_guards_for_path(self, *_: Any) -> list[dict[str, Any]]:
        self.reads.append("code_caller_guards")
        return self.live.get("code_caller_guards", [])

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, rows))

    def written(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t, rows in self.inserts:
            if rows:
                out[t] = out.get(t, 0) + len(rows)
        return out


async def _ingest(repo: _Repo, **kw: Any) -> ingest_stats.FileIngestStats:
    return await ingest.ingest_file(
        repo,
        "o",
        "code-ns",
        path="src/a.cpp",
        source=_SRC,
        commit_sha="c" * 40,
        embedding_model="m",
        embedder=None,
        **kw,
    )


async def _converged_repo() -> _Repo:
    """A repo holding exactly the rows a full ingest of _SRC writes."""
    first = _Repo()
    await _ingest(first)
    live: dict[str, list[dict[str, Any]]] = {}
    for t, rows in first.inserts:
        live.setdefault(t, []).extend(rows)
    for r in live.get("code_call_sites_unresolved", []):
        r.setdefault("disposition", r.get("disposition", ""))
    return _Repo(live)


def test_heal_scope_is_honoured_only_with_proof_of_unchanged_content() -> None:
    assert heal_scope(None, _SRC, _HASH) is None, "no scope: every stage"
    assert heal_scope(frozenset({"chunk"}), _SRC, _HASH) == {"chunk"}
    assert heal_scope(frozenset({"chunk", "bogus"}), _SRC, _HASH.upper()) == {"chunk"}, (
        "unknown stages drop, hex case-free"
    )
    assert heal_scope(frozenset({"chunk"}), _SRC, "") is None, "no recorded hash is not proof"
    assert heal_scope(frozenset({"chunk"}), _SRC + b"\n", _HASH) is None, (
        "content changed: every stage"
    )
    assert heal_scope(frozenset(), _SRC, _HASH) is None, "an empty scope is a heal, not a no-op"
    assert heal_scope(frozenset({"bogus"}), _SRC, _HASH) is None


@pytest.mark.asyncio
async def test_unscoped_ingest_runs_every_stage_and_writes_the_file_row() -> None:
    repo = _Repo()
    stats = await _ingest(repo)
    assert stats.stages_run == ("chunk", "symbol", "edge", "guard") and not stats.identical
    assert {
        "code_chunks",
        "code_symbols",
        "code_symbol_names",
        "code_edges",
        "code_call_sites_unresolved",
        "code_caller_guards",
    } <= set(repo.reads)
    assert repo.written().get("code_files") == 1
    assert repo.written().get("code_chunks", 0) >= 1 and repo.written().get("code_symbols", 0) >= 2


@pytest.mark.asyncio
async def test_chunk_scope_on_an_unchanged_converged_file_reads_chunks_only_and_writes_nothing() -> (
    None
):
    repo = await _converged_repo()
    stats = await _ingest(repo, stages=frozenset({"chunk"}), live_content_hash=_HASH)
    assert stats.stages_run == ("chunk",)
    assert repo.reads == ["code_chunks"], "one read: the chunk index (ids + spans)"
    assert repo.written() == {}, "identical chunk set: no rows, no file row"
    assert stats.identical is True


@pytest.mark.asyncio
async def test_chunk_scope_with_a_changed_chunk_set_writes_chunks_and_the_file_row_only() -> None:
    repo = await _converged_repo()
    # the live chunk set is from an older chunker: one id nobody produces now
    repo.live["code_chunks"] = [
        {"chunk_id": "stale-id", "start_line": 1, "end_line": 1, "version": 1}
    ]
    stats = await _ingest(repo, stages=frozenset({"chunk"}), live_content_hash=_HASH)
    assert stats.stages_run == ("chunk",) and not stats.identical
    assert "code_symbols" not in repo.reads and "code_edges" not in repo.reads
    written = repo.written()
    assert written.get("code_chunks", 0) >= 1 and written.get("code_files") == 1
    assert "code_symbols" not in written and "code_edges" not in written


@pytest.mark.asyncio
async def test_chunk_and_edge_scope_skips_the_symbol_block() -> None:
    repo = await _converged_repo()
    stats = await _ingest(repo, stages=frozenset({"chunk", "edge"}), live_content_hash=_HASH)
    assert stats.stages_run == ("chunk", "edge")
    assert "code_symbols" not in repo.reads, "the per-path symbol read is the skipped 75%"
    assert {"code_chunks", "code_symbol_names", "code_edges", "code_call_sites_unresolved"} <= set(
        repo.reads
    )
    assert "code_caller_guards" not in repo.reads, "the guard stage is not in the scope"


@pytest.mark.asyncio
async def test_scope_is_ignored_when_the_content_hash_does_not_match() -> None:
    repo = await _converged_repo()
    stats = await _ingest(repo, stages=frozenset({"chunk"}), live_content_hash="0" * 64)
    assert stats.stages_run == ("chunk", "symbol", "edge", "guard"), (
        "a changed file is a content repair"
    )
    assert "code_symbols" in repo.reads and "code_edges" in repo.reads
    assert repo.written().get("code_files") == 1, "an unscoped ingest always rewrites the file row"


@pytest.mark.asyncio
async def test_scoped_ingest_that_finds_a_symbol_change_writes_symbols_and_the_file_row() -> None:
    repo = await _converged_repo()
    repo.live["code_symbols"] = [dict(r, end_line=99) for r in repo.live["code_symbols"]]
    stats = await _ingest(repo, stages=frozenset({"symbol"}), live_content_hash=_HASH)
    assert stats.stages_run == ("symbol",) and not stats.identical
    assert stats.symbols_moved >= 1
    written = repo.written()
    assert written.get("code_symbols", 0) >= 1 and written.get("code_files") == 1
    assert "code_chunks" not in repo.reads and "code_edges" not in repo.reads


# ---------------------------------------------------------------------------
# the guard stage
# ---------------------------------------------------------------------------

_GUARDED_SRC = b"int f() { return 1; }\nint g(int x) { if (x) return f(); return 0; }\n"
_GUARDED_HASH = hashlib.sha256(_GUARDED_SRC).hexdigest()


@pytest.mark.asyncio
async def test_guard_scope_on_an_unchanged_file_reads_live_edges_and_guards_only() -> None:
    """A guard-only heal (call_guards.py moved, or a namespace whose stage
    keys predate the stage) must not re-resolve edges: its input is the
    path's live edge rows, its diff the path's live guard rows."""
    repo = await _converged_repo()
    stats = await _ingest(repo, stages=frozenset({"guard"}), live_content_hash=_HASH)
    assert stats.stages_run == ("guard",)
    assert repo.reads == ["code_edges", "code_caller_guards"], "two keyed reads, no name table"
    assert repo.written() == {} and stats.identical is True, (
        "no conditional call: no row, no file row"
    )


@pytest.mark.asyncio
async def test_guard_scope_writes_the_guard_rows_of_a_conditional_call_from_live_edges() -> None:
    """A namespace whose edge rows exist
    (the intra-file call g -> f resolves without a name table) and whose
    guard table is empty. The guard-only heal derives the row from the edge
    rows the first ingest wrote, never re-resolving them."""
    first = _Repo()
    stats0 = await ingest.ingest_file(
        first,
        "o",
        "code-ns",
        path="src/a.cpp",
        source=_GUARDED_SRC,
        commit_sha="c" * 40,
        embedding_model="m",
        embedder=None,
    )
    assert stats0.edges_new >= 1 and stats0.guards_new == 1, (
        "a full ingest writes the guard row itself"
    )
    live: dict[str, list[dict[str, Any]]] = {}
    for t, rows in first.inserts:
        live.setdefault(t, []).extend(rows)
    expected = live["code_caller_guards"]
    live["code_caller_guards"] = []
    repo = _Repo(live)
    stats = await ingest.ingest_file(
        repo,
        "o",
        "code-ns",
        path="src/a.cpp",
        source=_GUARDED_SRC,
        commit_sha="d" * 40,
        embedding_model="m",
        embedder=None,
        stages=frozenset({"guard"}),
        live_content_hash=_GUARDED_HASH,
    )
    assert stats.stages_run == ("guard",) and not stats.identical
    assert repo.reads == ["code_edges", "code_caller_guards"], (
        "live edges in, live guards to diff against"
    )
    assert stats.guards_new == 1 and stats.guards_tombstoned == 0
    rows = [r for t, rs in repo.inserts if t == "code_caller_guards" for r in rs]
    assert len(rows) == 1 and rows[0]["guards"] == ["x"] and rows[0]["guard_kinds"] == ["if"]
    assert rows[0]["sites"] == [2] and rows[0]["from_symbol_id"] == expected[0]["from_symbol_id"]
    assert rows[0]["site_callees"] == expected[0]["site_callees"], (
        "the same record the full ingest wrote"
    )
    assert repo.written().get("code_files") == 1, "a stage that wrote a row rewrites the file row"
    # converged: the same heal again keeps the row and writes nothing
    repo2 = _Repo(dict(repo.live, code_caller_guards=rows))
    stats2 = await ingest.ingest_file(
        repo2,
        "o",
        "code-ns",
        path="src/a.cpp",
        source=_GUARDED_SRC,
        commit_sha="e" * 40,
        embedding_model="m",
        embedder=None,
        stages=frozenset({"guard"}),
        live_content_hash=_GUARDED_HASH,
    )
    assert stats2.identical is True and repo2.written() == {}
