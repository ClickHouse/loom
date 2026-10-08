"""The term-frequency and doc-stats aggregates hold a
namespace's vocabulary and are erased with it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


class _FakeCH:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.counted: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        table = " ".join(sql.split()).split("FROM ")[1].split(" ")[0]
        self.counted.append(table)
        return {"n": self.counts.get(table, 0)}

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.executed.append((" ".join(sql.split()), dict(params or {})))

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "mutation_id": "mutation_1.txt",
                "is_done": 1,
                "parts_to_do": 0,
                "latest_fail_reason": "",
            }
        ]


def test_every_aggregate_table_is_deleted_by_namespace_and_empty_ones_are_skipped() -> None:
    from server.erasure.store.steps_memory import (
        TERM_AGGREGATE_TABLES,
        delete_term_aggregates_by_namespace,
    )

    assert set(TERM_AGGREGATE_TABLES) == {
        "loom.memory_term_freq",
        "loom.memory_doc_stats",
        "loom.chunk_term_freq",
    }
    ch = _FakeCH(
        {"loom.memory_term_freq": 120, "loom.memory_doc_stats": 0, "loom.chunk_term_freq": 7}
    )
    n = asyncio.run(delete_term_aggregates_by_namespace("o", "ns", ch=ch))
    assert n == 127
    assert ch.counted == list(TERM_AGGREGATE_TABLES)  # every table counted, in order
    deleted = [sql.split("ALTER TABLE ")[1].split(" ")[0] for sql, _ in ch.executed]
    assert deleted == ["loom.memory_term_freq", "loom.chunk_term_freq"]  # empty one skipped
    for sql, params in ch.executed:
        assert "DELETE WHERE (org = {org:String} AND namespace = {ns:String})" in sql
        assert "mutations_sync = 0" in sql
        assert {k: v for k, v in params.items() if k != "erasure_mutation_tag"} == {
            "org": "o",
            "ns": "ns",
        }


def test_namespace_saga_erases_the_aggregates_after_the_rows_they_derive_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app import saga as erasure

    names: list[str] = []

    async def _fake_step(
        *, step_name: str, step_fn: Any, completed: set[str], row: dict[str, Any], **kw: Any
    ) -> Any:
        names.append(step_name)
        return None

    monkeypatch.setattr(erasure, "_do_step", _fake_step)
    monkeypatch.setattr("server.erasure.app.backends.all_backends", lambda: [])
    asyncio.run(erasure._saga_namespace("o", "ns", completed=set(), row={}))
    assert "term_aggregates.delete" in names
    assert names.index("term_aggregates.delete") > names.index("memory_embeddings.tombstone")
    assert names.index("term_aggregates.delete") > names.index("procedure_runs.delete")
