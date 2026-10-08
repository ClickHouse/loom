"""The read-only memory ops the dashboard lists with, called through the
registry: `memory.agents_list`, `conflicts.allowlist_list`,
`learnings.quality` and `memory.stack`. CH-free: a fake client answers by
table and records every statement and its parameters."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME

_WHEN = datetime(2026, 1, 2, 3, 4, 5)


class _CH:
    def __init__(self, by_table: dict[str, list[dict[str, Any]]]) -> None:
        self.by_table = by_table
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        for table, rows in self.by_table.items():
            if f"FROM {table}" in sql:
                return [dict(r) for r in rows]
        return []

    def sql_for(self, table: str) -> list[tuple[str, dict[str, Any]]]:
        return [(s, p) for s, p in self.calls if f"FROM {table}" in s]


async def _call(name: str, **body: Any) -> Any:
    op = OPERATIONS_BY_NAME[name]
    return await op.handler(op.input_model(**body))


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch):
    def _install(by_table: dict[str, list[dict[str, Any]]]) -> _CH:
        fake = _CH(by_table)
        monkeypatch.setattr("server.core.ch.get_client", lambda: fake)
        monkeypatch.setattr("server.memory.app.stack.get_client", lambda: fake)
        return fake

    yield _install
    set_current_auth(None)


def test_the_four_ops_are_viewer_reads() -> None:
    for name in ("memory.agents_list", "conflicts.allowlist_list", "learnings.quality"):
        op = OPERATIONS_BY_NAME[name]
        assert op.required_role == Role.VIEWER and op.idempotent and not op.audit, name
    assert OPERATIONS_BY_NAME["memory.stack"].required_role == Role.VIEWER


async def test_agents_list_returns_the_live_agents_of_the_namespace(ch) -> None:
    fake = ch({"loom.memory_embeddings": [{"agent": "alpha"}, {"agent": ""}, {"agent": "beta"}]})
    out = await _call("memory.agents_list", org="o", namespace="ns")
    assert out.agents == ["alpha", "beta"]  # a blank agent is not listed
    ((sql, params),) = fake.sql_for("loom.memory_embeddings")
    assert "tombstoned_at IS NULL" in sql and "DISTINCT agent" in sql
    assert params == {"org": "o", "ns": "ns"}


async def test_allowlist_list_maps_rows_and_passes_the_limit(ch) -> None:
    fake = ch(
        {
            "loom.conflict_allowlist": [
                {
                    "memory_key_a": "a",
                    "memory_key_b": "b",
                    "allowed_at": _WHEN,
                    "allowed_by": "human:ops",
                    "reason": "both hold",
                }
            ]
        }
    )
    out = await _call("conflicts.allowlist_list", org="o", namespace="ns", limit=7)
    (row,) = out.rows
    assert (row.memory_key_a, row.memory_key_b, row.allowed_by, row.reason) == (
        "a",
        "b",
        "human:ops",
        "both hold",
    )
    assert row.allowed_at == _WHEN
    ((sql, params),) = fake.sql_for("loom.conflict_allowlist")
    assert "tombstoned_at IS NULL" in sql
    assert params == {"org": "o", "ns": "ns", "lim": 7}


_QUALITY_ROW = {"namespace": "ns", "pending": 3, "promoted": 2, "rejected": 2, "approve_ratio": 0.5}


async def test_learnings_quality_rolls_up_one_namespace(ch) -> None:
    fake = ch({"loom.learnings_queue": [_QUALITY_ROW]})
    out = await _call("learnings.quality", org="o", namespace="ns")
    (row,) = out.rows
    assert row.namespace == "ns" and row.pending == 3 and row.approve_ratio == 0.5
    ((sql, params),) = fake.sql_for("loom.learnings_queue")
    assert "namespace = {ns:String}" in sql and params["ns"] == "ns"


async def test_learnings_quality_without_a_namespace_sees_only_member_namespaces(
    ch, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ch({"loom.learnings_queue": [_QUALITY_ROW]})

    async def _mine(org: str) -> set[str]:
        return {"ns", "other"}

    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _mine)
    set_current_auth(
        AuthInfo(subject="alice", org="o", namespaces=("ns",), roles=(), is_service_account=False)
    )
    await _call("learnings.quality", org="o")
    ((sql, params),) = fake.sql_for("loom.learnings_queue")
    assert "namespace IN {ns_list:Array(String)}" in sql
    assert params["ns_list"] == ["ns", "other"]


async def test_learnings_quality_with_no_member_namespace_reads_nothing(
    ch, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ch({"loom.learnings_queue": [_QUALITY_ROW]})

    async def _none(org: str) -> set[str]:
        return set()

    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _none)
    set_current_auth(
        AuthInfo(subject="bob", org="o", namespaces=(), roles=(), is_service_account=False)
    )
    out = await _call("learnings.quality", org="o")
    assert out.rows == [] and fake.calls == []


async def test_memory_stack_orders_pinned_before_recent_and_skips_repeats(
    ch, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_profile(*a: Any, **k: Any) -> Any:
        raise RuntimeError("no profile in this test")

    monkeypatch.setattr("server.memory.app.profile.handle_profile_render", _no_profile)
    pinned = {"memory_key": "p1", "memory_type": "semantic", "value": "pinned fact"}
    fake = ch({})
    reads: list[str] = []

    async def query_all(sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        fake.calls.append((sql, dict(params or {})))
        if "pinned = 1" in sql:
            reads.append("pinned")
            return [pinned]
        if "recall_count > 0" in sql:
            reads.append("recent")
            return [
                {**pinned, "recall_count": 4},  # already served as pinned
                {
                    "memory_key": "r1",
                    "memory_type": "semantic",
                    "value": "recent",
                    "recall_count": 2,
                },
            ]
        return []

    fake.query_all = query_all  # type: ignore[method-assign]
    out = await _call(
        "memory.stack",
        org="o",
        namespace="ns",
        agent="ag",
        include_procedures=False,
        include_knowledge=False,
    )
    assert reads == ["pinned", "recent"]
    assert [(e.section, e.memory_key) for e in out.entries] == [
        ("pinned", "p1"),
        ("recent_recall", "r1"),
    ]
    assert out.section_counts["pinned"] == 1 and out.section_counts["recent_recall"] == 1
    assert out.tokens_used == sum(e.tokens for e in out.entries) <= out.tokens_budget
    for sql, params in fake.calls:
        assert "sensitivity != 'high'" in sql
        assert params["uid"] == "-" and params["sid"] == "-"
