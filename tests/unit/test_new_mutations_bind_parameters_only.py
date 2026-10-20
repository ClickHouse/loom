"""Mutations on the row-link tables name their rows by bound parameters only: ClickHouse
refuses a mutation with a subquery on a replicated table (code 36)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

_ROW = "00000000-0000-0000-0000-0000000000a1"


class _CH:
    """Answers every read with `rows` (a re-read of which memories are live: `live`;
    the drop's read after its DELETE: none); records every statement sent to execute."""

    def __init__(self, rows: list[dict[str, Any]], live: tuple[str, ...] = ()) -> None:
        self.rows = rows
        self.live = live
        self.executed: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "tombstoned_at IS NULL OR updated_at" in sql:
            return [{"id": i} for i in params["ids"] if i in self.live]
        if "sensitivity, entities" in sql:
            return []  # after the DELETE: the row is gone, so nothing is written back
        return self.rows

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        return {"n": 1}

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)


async def test_the_undeclared_edge_drop_deletes_by_parameters_only(monkeypatch) -> None:
    from server.memory.app import edge_links, projections

    async def no_edges(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(projections, "end_edges", no_edges)
    link = {"id": _ROW, "sha": "a" * 64, "entity_a": "alice", "entity_b": "carol"}
    ch = _CH([{**link, "entities": ["alice", "bob"]}])
    scope = SimpleNamespace(org="o", namespace="ns")
    await edge_links._drop_links_of(scope, {_ROW: {"alice", "bob"}}, {_ROW: 1}, ch=ch)
    assert ch.executed and all("DELETE FROM" in sql for sql in ch.executed)
    assert not [sql for sql in ch.executed if "SELECT" in sql]


async def test_the_orphan_row_link_delete_names_its_ids(monkeypatch) -> None:
    from server.erasure.store import mutations, steps_graph

    wheres: list[str] = []

    async def record(table: str, *, mutation: str = "", where: str, **kw: Any) -> None:
        wheres.append(where)

    monkeypatch.setattr(mutations, "run_mutation", record)
    monkeypatch.setattr(mutations, "run_lightweight_delete", record)
    ch = _CH([{"id": _ROW}])
    for scope_type, value in (("namespace", ["ns"]), ("user", "u1")):
        await steps_graph.delete_orphan_row_links(
            "o", scope_type, value, verified_at=datetime(2026, 1, 1), ch=ch
        )
    assert wheres and not [w for w in wheres if "SELECT" in w]
    assert all("memory_id IN {ids:Array(UUID)}" in w for w in wheres)


async def test_an_orphan_written_again_before_the_delete_keeps_its_links(monkeypatch) -> None:
    """Two orphan ids read; one memory is written again before the DELETE: only the
    other's links go."""
    from server.erasure.store import mutations, steps_graph

    deleted: list[list[str]] = []

    async def record(table: str, *, params: dict[str, Any], **kw: Any) -> None:
        deleted.append(params["ids"])

    monkeypatch.setattr(mutations, "run_mutation", record)
    monkeypatch.setattr(mutations, "run_lightweight_delete", record)
    other = "00000000-0000-0000-0000-0000000000b2"
    ch = _CH([{"id": _ROW}, {"id": other}], live=(other,))
    await steps_graph.delete_orphan_row_links(
        "o", "namespace", ["ns"], verified_at=datetime(2026, 1, 1), ch=ch
    )
    assert deleted == [[_ROW], [_ROW]]  # one DELETE per row-link table
