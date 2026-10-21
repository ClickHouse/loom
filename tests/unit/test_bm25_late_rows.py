"""A write whose embedding lands after the BM25 refresh watermark could have passed
its version indexes its own rows (`store/bm25_stats.py`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.store import bm25_stats
from tests.unit.test_memory_update_conflict import wired  # noqa: F401


@pytest.fixture(autouse=True)
def _no_delete_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """No delete races these writes: the fence before the insert finds no later version."""

    async def _none(*_a: Any, **_k: Any) -> dict[Any, int]:
        return {}

    monkeypatch.setattr("server.memory.store.versions_above", _none)


_NOW = 1_800_000_000 * 1_000_000_000
_S = 1_000_000_000


def _row(**kw: Any) -> dict[str, Any]:
    base = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"}
    return {**base, "memory_key": "k", "embedding_dim": 3, "tombstoned_at": None, **kw}


def test_only_live_vector_rows_older_than_half_the_lag_are_late() -> None:
    rows = [
        _row(memory_key="late", version=_NOW - 6 * _S),
        _row(memory_key="fresh", version=_NOW - 1 * _S),
        _row(memory_key="stub", version=_NOW - 60 * _S, embedding_dim=0),
        _row(memory_key="gone", version=_NOW - 60 * _S, tombstoned_at=datetime(2026, 1, 1)),
    ]
    assert [r["memory_key"] for r in bm25_stats.late_rows(rows, now_ns=_NOW)] == ["late"]


@pytest.mark.asyncio
async def test_late_rows_are_folded_with_both_views_statements_and_errors_are_logged() -> None:
    calls: list[tuple[str, Any]] = []

    class _CH:
        async def execute(self, sql: str, params: Any = None) -> None:
            calls.append((sql, params))

    old = _row(version=1)  # drawn long ago
    assert await bm25_stats.index_late_rows([old, _row(version=10**30)], ch=_CH()) == 1
    assert [("memory_doc_stats" in sql, "memory_term_freq" in sql) for sql, _ in calls] == [
        (True, False),
        (False, True),
    ]
    assert calls[0][1]["keys"] == [("o", "n", "a", "u", "s", "k")]

    class _Down:
        async def execute(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("ch down")

    assert await bm25_stats.index_late_rows([old], ch=_Down()) == 0


@pytest.mark.asyncio
async def test_both_statements_run_at_once_and_one_failing_waits_for_the_other(caplog) -> None:
    """Concurrent, and the call ends only after both have: shutdown's drain must not
    close the client under the statement that is still running."""
    import asyncio

    started: list[str] = []
    finished: list[str] = []
    both_started = asyncio.Event()

    class _CH:
        async def execute(self, sql: str, params: Any = None) -> None:
            table = "doc_stats" if "memory_doc_stats" in sql else "term_freq"
            started.append(table)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
            if table == "doc_stats":
                raise RuntimeError("memory limit")
            await asyncio.sleep(0.05)
            finished.append(table)

    with caplog.at_level("WARNING", logger=bm25_stats._log.name):
        assert await bm25_stats.index_late_rows([_row(version=1)], ch=_CH()) == 0
    assert sorted(started) == ["doc_stats", "term_freq"]
    assert finished == ["term_freq"]
    (warning,) = [r.getMessage() for r in caplog.records]
    assert "memory_doc_stats" in warning and "memory limit" in warning


def _late_and_blocked(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    """Every row late, and the statements held until released: a path that awaited them
    would not return."""
    import asyncio

    import server.memory.store as memory_repo

    release = asyncio.Event()
    indexed: list[str] = []

    async def _index(rows: Any, **_k: Any) -> int:
        await release.wait()
        indexed.extend(str(r.get("memory_key")) for r in rows)
        return len(rows)

    monkeypatch.setattr(memory_repo, "late_rows", lambda rows, **_k: list(rows))
    monkeypatch.setattr(memory_repo, "index_late_rows", _index)
    return release, indexed


@pytest.mark.asyncio
async def test_memory_set_answers_before_its_late_row_is_indexed(monkeypatch) -> None:
    import asyncio

    from server.memory.app import after_write
    from server.memory.app import write as write_mod
    from server.memory.domain.conflicts import ConflictResult
    from tests.unit.test_memory_set_verdict import _input, _wire

    _wire(monkeypatch, ConflictResult(verdict="clear", peers=[]))
    release, indexed = _late_and_blocked(monkeypatch)
    out = await asyncio.wait_for(write_mod.handle_memory_set(_input()), timeout=5)
    assert out.version > 0 and indexed == []
    release.set()
    await after_write.drain()
    assert indexed == ["release_day"]


@pytest.mark.asyncio
async def test_memory_update_answers_before_its_late_row_is_indexed(wired, monkeypatch) -> None:  # noqa: F811
    import asyncio

    from server.memory.app import after_write
    from server.memory.app import update as update_mod
    from server.memory.domain.conflicts import ConflictResult
    from tests.unit.test_memory_update_conflict import _input

    wired(ConflictResult(verdict="clear", peers=[]))
    release, indexed = _late_and_blocked(monkeypatch)
    out = await asyncio.wait_for(update_mod.handle_memory_update(_input()), timeout=5)
    assert out.updated and indexed == []
    release.set()
    await after_write.drain()
    assert indexed == ["deploy_day"]


@pytest.mark.asyncio
async def test_the_bulk_insert_and_the_worker_fill_index_their_late_rows(monkeypatch) -> None:
    """Each embedding write whose version predates its insert hands its rows over; a
    request does it after its response, so a slow statement does not hold the write."""
    import asyncio

    import server.memory.store as memory_repo
    from server.memory.app import after_write, bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.workers import embedding

    handed: list[list[dict[str, Any]]] = []
    release = asyncio.Event()

    async def _index(rows: Any, **_k: Any) -> int:
        await release.wait()  # inserts are slow
        handed.append(list(rows))
        return 0

    async def _noop(*_a: Any, **_k: Any) -> Any:
        return 0

    monkeypatch.setattr(memory_repo, "index_late_rows", _index)
    monkeypatch.setattr(memory_repo, "insert_memory_versions", _noop)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _noop)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _noop)
    state = _BulkWriteState()
    state.memory_rows.append({**_row(), "memory_type": "semantic", "sensitivity": "", "version": 1})
    state.embedding_rows.append(_row(version=1))
    await asyncio.wait_for(bulk_set._bulk_insert(object(), state), timeout=5)
    assert handed == []  # the write returned with the statements still running
    release.set()
    await after_write.drain()
    assert handed and handed[0][0]["memory_key"] == "k"

    # The worker: a filled stub is handed over after its replacement row is in (the
    # INSERT ... SELECT reads that row back).
    order: list[str] = []
    stub = {
        **_row(),
        "memory_type": "semantic",
        "scope": "private",
        "content_excerpt": "a stub",
        "embedding_model": "fake",
        "embed_attempts": 0,
        "version": 3,
    }

    async def _stubs(**_k: Any) -> list[dict[str, Any]]:
        return [dict(stub)]

    async def _none(**_k: Any) -> list[Any]:
        return []

    async def _insert(rows: Any, **_k: Any) -> None:
        if rows:
            order.append("insert")

    async def _index_after(rows: Any, **_k: Any) -> int:
        order.append("index")
        return 0

    async def _embed(_e: Any, texts: list[str]) -> list[tuple[list[float], str]]:
        return [([0.1, 0.2, 0.3], "") for _ in texts]

    class _Embedder:
        model_name, dim = "fake", 3

    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", _none)
    monkeypatch.setattr(memory_repo, "find_embedding_stubs", _stubs)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _insert)
    monkeypatch.setattr(memory_repo, "index_late_rows", _index_after)
    monkeypatch.setattr(embedding, "_embed_batch", _embed)
    monkeypatch.setattr(embedding, "_refresh_entities", _noop)
    monkeypatch.setattr(embedding, "get_embedder", lambda: _Embedder())
    await embedding.run_pass(embedding.EmbeddingWorkerConfig(org_filter="o", namespace_filter="n"))
    assert order[:2] == ["insert", "index"], order


@pytest.mark.asyncio
async def test_a_write_with_no_late_row_schedules_nothing() -> None:
    """The common write lands inside the lag: no task, no statement."""
    from server.memory.app import after_write

    assert after_write.index_late_rows([_row(version=10**30)], ch=object()) is None
    assert after_write.index_late_rows([], ch=object()) is None


@pytest.mark.asyncio
async def test_the_dim0_repair_and_reindex_index_their_late_rows(monkeypatch) -> None:
    """Both write vectors under the BM25 watermark (+ 1 of a row read, or the memory
    row's version): each hands its inserted rows to index_late_rows."""
    import server.memory.store as memory_repo
    from server.memory.store import integrity

    handed: list[str] = []

    async def _index(rows: Any, **_k: Any) -> int:
        handed.extend(r["memory_key"] for r in rows)
        return 0

    async def _noop(*_a: Any, **_k: Any) -> Any:
        return 0

    monkeypatch.setattr(bm25_stats, "index_late_rows", _index)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _noop)
    await integrity.repair_dim0_rows(
        [{**_row(memory_key="dim0"), "embedding": [0.1, 0.2], "version": 1}], ch=object()
    )
    assert handed == ["dim0"]
