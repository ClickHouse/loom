"""Mutations name their rows by bound parameters only: ClickHouse refuses a mutation with
a subquery or a non-deterministic function (now(), now64(), rand()) on a replicated table
(code 36). Every erasure saga step, the row-link deletes, the drop's undo of its edge ends
and `memory.clear` are held to it."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.leases import LOCK_TABLE
from tests.unit.erasure_saga_stub import ns_list as _ns_list
from tests.unit.erasure_saga_stub import saga_runner

_ROW = "00000000-0000-0000-0000-0000000000a1"
_MUTATION = re.compile(r"^\s*(?:ALTER TABLE\s+(\S+)\s+(?:UPDATE|DELETE)\b|DELETE FROM\s+(\S+))")
_NONDETERMINISTIC = re.compile(r"\b(?:now|now64|today|rand\w*|generateUUID\w*)\(")
_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "schema.sql"
# A KeeperMap mutation is a key write Keeper applies at once, not a replicated part rewrite.
_KEEPER_MAP = {
    LOCK_TABLE,  # created by the lease code, not a migration
    *re.findall(
        r"CREATE TABLE IF NOT EXISTS (\S+)\s*\([^;]*?ENGINE = KeeperMap", _SCHEMA.read_text()
    ),
}


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Any:
    return saga_runner(monkeypatch)


def _refused(statements: list[str]) -> list[str]:
    """The mutations a replicated table would refuse."""
    out = []
    for sql in statements:
        m = _MUTATION.match(sql)
        if not m:
            continue
        table = m.group(1) or m.group(2)
        if "SELECT" in sql or (table not in _KEEPER_MAP and _NONDETERMINISTIC.search(sql)):
            out.append(" ".join(sql.split()))
    return out


@pytest.mark.parametrize("purge", [False, True])
async def test_every_user_erasure_step_mutates_by_bound_parameters_only(
    run: Any,
    purge: bool,
) -> None:
    """The user saga end to end: every read answers one row, so every step reaches every
    mutation it can issue, each slice of it included."""
    await run("user_id", "alice", purge=purge)
    mutations = [sql for sql in run.ch.executed if _MUTATION.match(sql)]
    assert len(mutations) > 40  # the stub's rows took every step to its mutations
    assert _refused(mutations) == []


@pytest.mark.parametrize(
    ("scope_type", "scope_value"),
    [("namespace", "a"), ("namespace_list", _ns_list(purge=True, external=False))],
)
async def test_every_namespace_erasure_step_mutates_by_bound_parameters_only(
    run: Any,
    scope_type: str,
    scope_value: str,
) -> None:
    await run(scope_type, scope_value)
    assert _refused(run.ch.executed) == []


def test_the_check_catches_a_subquery_and_now() -> None:
    assert _refused(
        [
            "ALTER TABLE loom.memory DELETE WHERE k IN (SELECT k FROM loom.memory)",
            "DELETE FROM loom.memory WHERE lock_until <= now()",
            "ALTER TABLE loom.blacklist UPDATE last_hit_at = now() WHERE 1",
            "ALTER TABLE loom.memory DELETE WHERE k IN {keys:Array(String)}",
        ]
    ) == [
        "ALTER TABLE loom.memory DELETE WHERE k IN (SELECT k FROM loom.memory)",
        "DELETE FROM loom.memory WHERE lock_until <= now()",
        "ALTER TABLE loom.blacklist UPDATE last_hit_at = now() WHERE 1",
    ]
    assert "loom._migration_lock" in _KEEPER_MAP


@pytest.mark.parametrize("locked", [0, 1])
async def test_a_clear_mutates_by_bound_parameters_only(locked: int) -> None:
    from server.memory.store.lifecycle import bulk_tombstone_scope

    class _ClearCH(_CH):
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            return {"n": locked if "lock_mode != ''" in sql else 1}

    ch = _ClearCH([{"h": 1}])  # the locked identities, read as hashes
    counts = await bulk_tombstone_scope(org="o", namespace="ns", user_id="u", ch=ch)
    assert counts["loom.memory"] == 1 and counts["loom.memory_embeddings"] == 1
    assert len(ch.executed) == 2 and _refused(ch.executed) == []


class _CH:
    """Answers every read with `rows`; records every statement sent to execute."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
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
    monkeypatch.setattr(edge_links, "_restore_kept_links", no_edges)
    link = {"id": _ROW, "sha": "a" * 64, "entity_a": "alice", "entity_b": "carol"}
    ch = _CH([{**link, "entities": ["alice", "bob"]}])
    scope = SimpleNamespace(org="o", namespace="ns")
    await edge_links._drop_links_of(
        scope, {_ROW: {"alice", "bob"}}, {_ROW: 1}, ch=ch, op="memory.set"
    )
    assert ch.executed and all("DELETE FROM" in sql for sql in ch.executed)
    assert not [sql for sql in ch.executed if "SELECT" in sql]


async def test_the_orphan_row_link_delete_names_its_ids(monkeypatch) -> None:
    from server.erasure.store import mutations, steps_graph

    wheres: list[str] = []

    async def record(table: str, *, mutation: str = "", where: str, **kw: Any) -> None:
        wheres.append(where)

    async def none_kept(*a: Any, **kw: Any) -> set[str]:
        return set()

    monkeypatch.setattr(mutations, "run_mutation", record)
    monkeypatch.setattr(mutations, "run_lightweight_delete", record)
    monkeypatch.setattr(steps_graph, "_live_or_written_since", none_kept)
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

    other = "00000000-0000-0000-0000-0000000000b2"

    async def other_written_again(org: str, orphans: Any, ids: list[str], **kw: Any) -> set[str]:
        return {other} & set(ids)

    monkeypatch.setattr(mutations, "run_mutation", record)
    monkeypatch.setattr(mutations, "run_lightweight_delete", record)
    monkeypatch.setattr(steps_graph, "_live_or_written_since", other_written_again)
    ch = _CH([{"id": _ROW}, {"id": other}])
    await steps_graph.delete_orphan_row_links(
        "o", "namespace", ["ns"], verified_at=datetime(2026, 1, 1), ch=ch
    )
    assert deleted == [[_ROW], [_ROW]]  # one DELETE per row-link table


async def test_the_edge_undo_binds_parameters_only() -> None:
    from server.memory.app import projections

    ch = _CH([])
    await projections.reopen_closed_by(
        ch, org="o", namespace="ns", close_ids=["c1"], pairs=[("alice", "carol")]
    )
    assert len(ch.executed) == 1 and "SELECT" not in ch.executed[0]


async def test_the_document_blob_sweep_mutates_by_bound_parameters_only() -> None:
    """The original sweep's deletes of `loom.document_blobs` and `loom.blob_uploads`."""
    from server.knowledge.store import blobs

    class _Recorder:
        def __init__(self) -> None:
            self.executed: list[str] = []

        async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
            return {"cutoff_ms": 0}

        async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[Any]:
            return []

        async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
            self.executed.append(sql)

    ch = _Recorder()
    await blobs.delete("o", "ns", ["a" * 64], min_age_s=3600, ch=ch)
    await blobs.forget_uploads([("o", "ns", "a" * 64)], 3600, ch=ch)
    assert {s.split()[2] for s in ch.executed} == {"loom.document_blobs", "loom.blob_uploads"}
    assert _refused(ch.executed) == []
