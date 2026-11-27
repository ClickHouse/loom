"""The in-database re-embed covers every chunk of a path, however many chunks
the path holds: batches end on whole paths, and one path wider than a batch is
read whole."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import reembed_model as rm
from server.code.domain.extract import lang_reference as lr
from server.code.store.index import CodeRepo


def _row(path: str, i: int) -> dict[str, Any]:
    return {
        "path": path,
        "chunk_id": f"{path}#{i:06d}",
        "scope": "",
        "kind": "function",
        "start_line": i,
        "end_line": i + 1,
        "content": f"c{i}",
        "content_hash": "h",
        "context_text": f"t{i}",
        "context_description": "",
        "context_source": "",
        "lexemes": "",
        "symbol_ids": [],
        "indexed_commit": "abc",
        "embed_last_error": "",
    }


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = sorted(rows, key=lambda r: (r["path"], r["chunk_id"]))

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "SELECT path, chunk_id FROM" in sql:
            return []  # nothing under the target label yet
        if "only" in params:
            return [r for r in self.rows if r["path"] == params["only"]]
        out = [r for r in self.rows if r["path"] > params["after"]]
        return out[: params["limit"]]


class _Repo(CodeRepo):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(_CH(rows))  # type: ignore[arg-type]
        self.written: list[str] = []
        self.rows: list[dict[str, Any]] = []

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.written.extend(str(r["chunk_id"]) for r in rows)
        self.rows.extend(rows)


class _Embedder:
    model_name = "second-model"
    dim = 2

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[0.1, 0.2] for _ in texts]


@pytest.mark.parametrize("batch_rows", [3, 10])
def test_every_chunk_is_written_once_even_for_a_path_wider_than_any_batch(
    batch_rows: int,
) -> None:
    rows = [_row("a.cpp", i) for i in range(2)]
    rows += [_row("huge.cpp", i) for i in range(batch_rows * 20 + 7)]  # wider than any read
    rows += [_row("z.cpp", i) for i in range(4)]
    repo = _Repo(rows)
    stats = asyncio.run(
        rm.reembed_namespace(
            repo,  # type: ignore[arg-type]
            "o",
            "n",
            source_model="served-model",
            embedder=_Embedder(),
            batch_rows=batch_rows,
        )
    )
    assert sorted(repo.written) == sorted(r["chunk_id"] for r in rows)
    assert len(repo.written) == len(set(repo.written))
    assert stats.rows_written == len(rows) and stats.paths_seen == 3


def test_a_row_unembedded_by_design_is_copied_with_its_marker_and_no_vector() -> None:
    excluded, fixture = lr.UNEMBEDDED_EMBED_EXCLUDE, lr.UNEMBEDDED_BY_DESIGN
    rows = [_row("a.py", 0), _row("a_test.py", 1), _row("q.reference", 2)]
    rows[1]["embed_last_error"], rows[2]["embed_last_error"] = excluded, fixture
    repo, embedder = _Repo(rows), _Embedder()
    stats = asyncio.run(
        rm.reembed_namespace(
            repo,  # type: ignore[arg-type]
            "o",
            "n",
            source_model="served-model",
            embedder=embedder,
        )
    )
    assert embedder.texts == ["t0"] and stats.rows_written == 3
    assert {(r["path"], r["embedding_dim"], r["embed_last_error"]) for r in repo.rows} == {
        ("a.py", 2, ""),
        ("a_test.py", 0, excluded),
        ("q.reference", 0, fixture),
    }
    assert {r["embedding_model"] for r in repo.rows} == {"second-model"}
