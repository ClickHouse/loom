"""`FileIngestStats.error` must be ASSIGNED, not just declared.

It is read once (`index_error=stats.error`); if a parse failure only logged
and returned a bodiless ParsedFile, `code_files.index_error` would be '' for
every file and `code.index_errors`, `code.coverage.files_errored` and the dashboard's
Health tile would be structurally empty. These tests drive
`ingest_file` through each failure path with a stub repo and check the error
reaches the persisted file row, bounded.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.index import ingest, ingest_chunks, ingest_edges, ingest_parse, ingest_stats


class _StubRepo:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def live_chunks_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def live_chunk_index_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def chunks_full_by_ids(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def live_symbols_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def live_edges_for_path(self, *_: Any) -> list[dict[str, Any]]:
        return []

    async def name_candidates(self, *_: Any) -> dict[str, Any]:
        return {}

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, rows))

    def file_rows(self) -> list[dict[str, Any]]:
        return [r for t, rows in self.inserts if t == "code_files" for r in rows]


_SRC = b"int f() { return 1; }\nint g() { return f(); }\n"


async def _ingest(repo: _StubRepo, path: str = "src/a.cpp") -> ingest_stats.FileIngestStats:
    return await ingest.ingest_file(
        repo,
        "o",
        "code-ns",
        path=path,
        source=_SRC,
        commit_sha="c" * 40,
        embedding_model="m",
        embedder=None,
    )


@pytest.mark.asyncio
async def test_parse_failure_lands_in_index_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(path: str, source: bytes) -> Any:
        raise RuntimeError("grammar exploded")

    monkeypatch.setitem(ingest_parse._PARSERS, "cpp", _boom)
    repo = _StubRepo()
    stats = await _ingest(repo)

    assert not stats.skipped
    assert stats.error == "parse: RuntimeError"  # the text is in the log
    # Degraded to chunk-only: the file is still searchable...
    assert any(t == "code_chunks" and rows for t, rows in repo.inserts)
    assert stats.symbols_new == 0
    # ...and the file row carries the reason, which is what code.index_errors
    # and code.coverage.files_errored read.
    (file_row,) = repo.file_rows()
    assert file_row["index_error"] == stats.error
    assert file_row["chunk_count"] >= 1


@pytest.mark.asyncio
async def test_chunk_failure_writes_only_the_file_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RecursionError case: the commit must not abort, the file must not
    vanish from every report."""

    def _boom(pf: Any, max_chars: int = 0, **_kw: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ingest_chunks, "chunk_file", _boom)
    repo = _StubRepo()
    stats = await _ingest(repo)

    assert stats.error.startswith("chunk: RecursionError")
    assert stats.chunks_new == 0
    tables = [t for t, _ in repo.inserts]
    assert tables == ["code_files"]
    (file_row,) = repo.file_rows()
    assert file_row["index_error"] == stats.error
    assert file_row["chunk_count"] == 0


@pytest.mark.asyncio
async def test_edge_failure_keeps_chunks_and_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(pf: Any, header: Any = None) -> Any:
        raise ValueError("bad tree")

    monkeypatch.setattr(ingest_edges, "collect_call_names", _boom)
    repo = _StubRepo()
    stats = await _ingest(repo)

    assert stats.error == "edges: ValueError"
    tables = [t for t, _ in repo.inserts]
    assert "code_chunks" in tables and "code_symbols" in tables
    assert stats.edges_new == 0
    (file_row,) = repo.file_rows()
    assert file_row["index_error"] == stats.error


def test_error_text_names_the_stage_and_the_kind_only() -> None:
    """`code.index_errors` returns the column to a viewer: a stage's exception can be
    a ClickHouse error with its statement, so the kind only."""
    from server.core.errors import BackendUnavailableError

    driver = RuntimeError("Code: 210. Connection refused (ch-internal-7:9440)\nSELECT 1")
    assert ingest_stats.error_text("parse", driver) == "parse: RuntimeError"
    typed = BackendUnavailableError("ClickHouse is unavailable; retry")
    assert ingest_stats.error_text("edges", typed) == (
        "edges: BACKEND_UNAVAILABLE: ClickHouse is unavailable; retry"
    )


def test_parse_file_public_shape_unchanged() -> None:
    """`parse_file` callers (chunker tests, backfill) still get a ParsedFile
    or None; the error travels on the private tuple form only."""
    # LICENSE is a text file the index keeps; a binary stays out
    assert ingest_parse.parse_file("img/logo.png", b"\x89PNG\x00") is None
    assert ingest_parse.parse_file("LICENSE", b"text").lang == "text"
    pf = ingest_parse.parse_file("a.cpp", _SRC)
    assert pf is not None and pf.lang == "cpp"


@pytest.mark.asyncio
async def test_success_leaves_index_error_empty() -> None:
    repo = _StubRepo()
    stats = await _ingest(repo)
    assert stats.error == ""
    (file_row,) = repo.file_rows()
    assert file_row["index_error"] == ""


def test_clip_for_embedding_bounds_and_marks() -> None:
    from server.code.app.index.embed_text import EMBED_MAX_CHARS, clip_for_embedding

    assert clip_for_embedding("short") == "short"
    big = "x" * (EMBED_MAX_CHARS * 3)
    out = clip_for_embedding(big)
    assert len(out) == EMBED_MAX_CHARS and out.endswith("[truncated for embedding]")


@pytest.mark.asyncio
async def test_oversize_file_is_recorded_not_chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 14 MB JSON fixture produced one chunk with a 14 MB embedding input, a
    provider 400 and eight recovery retries. Above the cap the file row records
    why and nothing else is written."""
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ingest_max_file_bytes", 100)
    repo = _StubRepo()
    stats = await ingest.ingest_file(
        repo,
        "o",
        "code-ns",
        path="tests/data/big.json",
        source=b"{" + b"x" * 500 + b"}",
        commit_sha="c" * 40,
        embedding_model="m",
        embedder=None,
    )
    assert stats.error.startswith("skipped: 502 bytes > code_ingest_max_file_bytes")
    assert [t for t, _ in repo.inserts] == ["code_files"]
    (row,) = repo.file_rows()
    assert row["index_error"] == stats.error and row["chunk_count"] == 0


@pytest.mark.asyncio
async def test_embedding_input_is_clipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index.embed_text import EMBED_MAX_CHARS

    seen: list[int] = []

    class _Embedder:
        model_name = "m"

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            seen.extend(len(t) for t in texts)
            return [[1.0] for _ in texts]

    monkeypatch.setattr(
        ingest_chunks,
        "chunk_file",
        lambda pf, max_chars=0, **_kw: [
            type(
                "C",
                (),
                {
                    "content": "y" * 50_000,
                    "content_hash": "h" * 64,
                    "scope": "",
                    "kind": "other",
                    "start_line": 1,
                    "end_line": 2,
                    "lexemes": [],
                    "symbol_ids": (),
                    "chunk_id": lambda self, ns: "0" * 32,
                },
            )()
        ],
    )
    repo = _StubRepo()
    await ingest.ingest_file(
        repo,
        "o",
        "code-ns",
        path="src/a.cpp",
        source=_SRC,
        commit_sha="c" * 40,
        embedding_model="m",
        embedder=_Embedder(),
    )
    assert seen and max(seen) <= EMBED_MAX_CHARS


def test_clip_for_embedding_uses_the_token_counter() -> None:
    from server.code.app.index.embed_text import clip_for_embedding

    dense = "1234567890," * 1_500  # 16.5k chars; pretend 1.5 chars/token
    out = clip_for_embedding(dense, count_tokens=lambda t: int(len(t) / 1.5), max_tokens=8_000)
    assert int(len(out) / 1.5) <= 8_000
    assert out.endswith("[truncated for embedding]")
    # a counter that raises degrades to the char clip, never to an exception
    assert clip_for_embedding("ok", count_tokens=lambda t: 1 // 0) == "ok"


@pytest.mark.asyncio
async def test_embed_shrinking_halves_on_provider_length_rejection() -> None:
    from server.code.app.index.embed_text import embed_shrinking

    calls: list[int] = []

    class _E:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            (text,) = texts
            calls.append(len(text))
            if len(text) > 3_000:
                # what httpx actually surfaces: no body, just the status line
                raise RuntimeError(
                    "Client error '400 Bad Request' for url 'https://api.openai.com/v1/embeddings'"
                )
            return [[1.0]]

    assert await embed_shrinking(_E(), "z" * 20_000) == [1.0]
    assert calls[0] == 20_000 and calls[-1] <= 3_000 and len(calls) <= 5


@pytest.mark.asyncio
async def test_embed_shrinking_propagates_other_errors() -> None:
    from server.code.app.index.embed_text import embed_shrinking

    class _E:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("401 Unauthorized")

    with pytest.raises(RuntimeError, match="401"):
        await embed_shrinking(_E(), "z" * 20_000)


@pytest.mark.asyncio
async def test_deep_recursion_retries_once_under_a_deeper_limit_and_restores_it() -> None:
    """A generated table nests past the default recursion limit in the edge
    walk; the stage runs again under a deeper limit and the limit is put
    back afterwards."""
    import sys

    from server.code.app.index import ingest_edges

    before = sys.getrecursionlimit()
    calls = {"n": 0}

    def _deep(n: int) -> int:
        return 0 if n == 0 else 1 + _deep(n - 1)

    async def _fn() -> int:
        calls["n"] += 1
        return _deep(before + 500)  # deeper than the current limit allows

    assert await ingest_edges._with_deep_recursion(_fn) == before + 500
    assert calls["n"] == 2 and sys.getrecursionlimit() == before

    async def _boom() -> int:
        raise ValueError("not a recursion problem")

    with pytest.raises(ValueError):
        await ingest_edges._with_deep_recursion(_boom)
