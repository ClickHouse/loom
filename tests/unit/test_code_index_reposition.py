"""Positional staleness: an edit ABOVE a function leaves its content (chunk id,
symbol key) unchanged while its line numbers shift. Keeping the live row on
key alone would leave spans stale for as long as the content stayed
identical, so chunks are re-versioned with the new span (embedding carried
over), and symbols are rewritten when span/signature/kind differ."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import ingest, ingest_stats
from server.code.app.index.scip_delta import ScipApplyStats, ScipFile, ScipSymbol, _apply_file


class _Repo:
    def __init__(self, chunks=(), symbols=()) -> None:
        self.chunks, self.symbols = list(chunks), list(symbols)
        self.full_chunk_reads: list[list[str]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def live_chunks_for_path(self, *_: Any):
        return self.chunks

    async def live_chunk_index_for_path(self, *_: Any):
        # The reconcile reads only ids and spans …
        return [
            {k: r[k] for k in ("chunk_id", "start_line", "end_line", "kind", "version") if k in r}
            for r in self.chunks
        ]

    async def chunks_full_by_ids(self, org, ns, path, model, chunk_ids):
        # … and asks for the full rows of just the chunks it rewrites.
        self.full_chunk_reads.append(sorted(chunk_ids))
        return [r for r in self.chunks if str(r["chunk_id"]) in set(chunk_ids)]

    async def live_symbols_for_path(self, *_: Any):
        return self.symbols

    async def live_edges_for_path(self, *_: Any):
        return []

    async def name_candidates(self, *_: Any):
        return {}

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, rows))

    def rows(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rs in self.inserts if t == table for r in rs]


V1 = b"int f() {\n  return 1;\n}\n\nint g() {\n  return f();\n}\n"
# two lines inserted at the top: f and g keep their content, move down by 2
V2 = b"// new comment\n// another\n" + V1


def _ingest(repo: _Repo, src: bytes) -> ingest_stats.FileIngestStats:
    return asyncio.run(
        ingest.ingest_file(
            repo,
            "o",
            "code-ns",
            path="src/a.cpp",
            source=src,
            commit_sha="c" * 40,
            embedding_model="m",
            embedder=None,
        )
    )


def test_fast_tier_refreshes_spans_of_moved_chunks_and_symbols():
    first = _Repo()
    s1 = _ingest(first, V1)
    chunks1 = [r for r in first.rows("code_chunks") if r.get("tombstoned_at") is None]
    syms1 = [r for r in first.rows("code_symbols") if r.get("tombstoned_at") is None]
    assert s1.chunks_new == len(chunks1) >= 1 and s1.symbols_new == len(syms1) >= 2
    # pretend the first pass' rows are live and carry a real embedding
    for r in chunks1:
        r["embedding"] = [0.5] * 4
        r["embedding_dim"] = 4

    second = _Repo(chunks=chunks1, symbols=syms1)
    s2 = _ingest(second, V2)
    live_chunks = [r for r in second.rows("code_chunks") if r.get("tombstoned_at") is None]
    # no re-embedding, no new ids: every chunk of V1 still exists, moved by 2
    assert s2.chunks_new == 0 or all(
        r["chunk_id"] not in {c["chunk_id"] for c in chunks1}
        for r in live_chunks
        if r["embedding_dim"] == 0
    )
    moved = [r for r in live_chunks if r["embedding_dim"] == 4]
    assert s2.chunks_moved == len(moved) >= 1, "moved chunks re-versioned with embedding carried"
    by_id = {c["chunk_id"]: c for c in chunks1}
    for r in moved:
        assert r["start_line"] == by_id[r["chunk_id"]]["start_line"] + 2
        assert r["end_line"] == by_id[r["chunk_id"]]["end_line"] + 2
        assert r["indexed_commit"] == "c" * 40 and r["version"] > by_id[r["chunk_id"]]["version"]
    # symbols: same keys, rewritten with the new span, nothing tombstoned
    live_syms = [r for r in second.rows("code_symbols") if r.get("tombstoned_at") is None]
    tomb_syms = [r for r in second.rows("code_symbols") if r.get("tombstoned_at") is not None]
    assert s2.symbols_moved == len(live_syms) == len(syms1) and not tomb_syms
    old = {(r["qualified_name"], r["symbol_id"]): r for r in syms1}
    for r in live_syms:
        o = old[(r["qualified_name"], r["symbol_id"])]
        assert (r["start_line"], r["end_line"]) == (o["start_line"] + 2, o["end_line"] + 2)

    # the full rows (vector included) were fetched only for the chunks that
    # were rewritten — the reconcile itself read ids and spans
    gone = [r["chunk_id"] for r in second.rows("code_chunks") if r.get("tombstoned_at")]
    assert second.full_chunk_reads == [sorted({*(r["chunk_id"] for r in moved), *gone})]

    # and an identical re-ingest is a no-op on both tables
    third = _Repo(chunks=live_chunks, symbols=live_syms)
    s3 = _ingest(third, V2)
    assert (
        s3.chunks_moved == 0
        and s3.chunks_new == 0
        and s3.symbols_moved == 0
        and s3.symbols_new == 0
    )


@pytest.mark.asyncio
async def test_scip_tier_rewrites_moved_symbol_and_keeps_unmoved():
    sym_a = ScipSymbol(
        qualified_name="DB::a",
        signature="void a()",
        name="a",
        kind="function",
        line_start=10,
        line_end=10,
    )
    sym_b = ScipSymbol(
        qualified_name="DB::b",
        signature="void b()",
        name="b",
        kind="function",
        line_start=30,
        line_end=30,
    )
    from server.code.app.index.scip_delta import symbol_id

    live = [
        {
            "qualified_name": "DB::a",
            "symbol_id": symbol_id("DB::a", "void a()"),
            "start_line": 10,
            "end_line": 10,
            "kind": "function",
            "path": "src/x.cpp",
            "tier": "scip",
            "signature": "void a()",
        },
        {
            "qualified_name": "DB::b",
            "symbol_id": symbol_id("DB::b", "void b()"),
            "start_line": 25,
            "end_line": 25,
            "kind": "function",
            "path": "src/x.cpp",
            "tier": "scip",
            "signature": "void b()",
        },  # stale: moved 25 -> 30
    ]
    repo = _Repo(symbols=live)
    stats = ScipApplyStats()
    await _apply_file(
        repo, "o", "ns", "d" * 40, ScipFile(path="src/x.cpp", symbols=[sym_a, sym_b]), stats
    )
    written = [r for r in repo.rows("code_symbols") if r.get("tombstoned_at") is None]
    assert [(r["qualified_name"], r["start_line"]) for r in written] == [("DB::b", 30)], (
        "only the moved symbol is rewritten"
    )
    assert not [r for r in repo.rows("code_symbols") if r.get("tombstoned_at") is not None]


def test_fast_tier_refreshes_the_kind_of_a_content_identical_chunk():
    """A chunk id is content-addressed, so a chunker that
    files the same text under another kind (a wrapped component: skeleton
    windows → `function`) changes nothing the key sees; the reconcile
    re-versions the row with the new kind, embedding carried."""
    first = _Repo()
    _ingest(first, V1)
    chunks1 = [r for r in first.rows("code_chunks") if r.get("tombstoned_at") is None]
    fn = [r for r in chunks1 if r["kind"] == "function"]
    assert fn
    for r in chunks1:
        r["embedding"] = [0.5] * 4
        r["embedding_dim"] = 4
    fn[0]["kind"] = "class_skeleton"  # the live row carries the chunker's old verdict
    second = _Repo(chunks=chunks1, symbols=[])
    s2 = _ingest(second, V1)
    live = [r for r in second.rows("code_chunks") if r.get("tombstoned_at") is None]
    assert s2.chunks_moved == 1 and s2.chunks_new == 0
    assert [r["kind"] for r in live] == ["function"] and live[0]["chunk_id"] == fn[0]["chunk_id"]
    assert live[0]["embedding_dim"] == 4 and live[0]["version"] > fn[0]["version"]


def test_fast_tier_rewrites_a_symbol_whose_return_type_is_now_known():
    """The id is name|signature and the span is unchanged; the
    parser states a return type the live row lacks — the row is
    re-versioned, as it is for a changed kind."""
    src = b"export function make(res) { return new Reply(res); }\n"
    first = _Repo()
    _ingest_ts(first, src)
    syms1 = [r for r in first.rows("code_symbols") if r.get("tombstoned_at") is None]
    assert syms1 and syms1[0]["return_type"] == "Reply"
    syms1[0]["return_type"] = ""  # a row written without a return type
    second = _Repo(chunks=[], symbols=syms1)
    s2 = _ingest_ts(second, src)
    live = [r for r in second.rows("code_symbols") if r.get("tombstoned_at") is None]
    assert s2.symbols_moved == 1 and live[0]["return_type"] == "Reply"


def _ingest_ts(repo: _Repo, src: bytes) -> ingest_stats.FileIngestStats:
    return asyncio.run(
        ingest.ingest_file(
            repo,
            "o",
            "code-ns",
            path="src/a.ts",
            source=src,
            commit_sha="c" * 40,
            embedding_model="m",
            embedder=None,
        )
    )
