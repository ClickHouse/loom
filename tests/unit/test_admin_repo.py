"""Unit tests for server/insights/store/admin.py."""

from __future__ import annotations

from typing import Any

import pytest

from server.insights.store import admin as admin_repo


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        raise AssertionError("not used")

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


@pytest.mark.asyncio
async def test_list_loom_tables_filters_inner_bookkeeping() -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {"name": "memory"},
            {"name": ".inner_id.some-mv"},
            {"name": "audit_log"},
        ]
    )
    out = await admin_repo.list_loom_tables(ch=ch)
    assert "memory" in out
    assert "audit_log" in out
    # CH MV inner_id bookkeeping is excluded.
    assert all(not n.startswith(".inner") for n in out)


@pytest.mark.asyncio
async def test_audit_summary_optional_filters() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await admin_repo.audit_summary(3600, ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "GROUP BY event_type" in sql
    assert "ORDER BY cnt DESC" in sql
    # No org/namespace filter when not supplied.
    assert "org = {p_org:String}" not in sql


@pytest.mark.asyncio
async def test_events_tail_threads_all_filters() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await admin_repo.events_tail(
        60,
        100,
        filters={
            "org": "clickhouse",
            "namespace": "ns",
            "agent": "bot",
            "operation": "memory.set",
            "success": False,
        },
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "org = {p_org:String}" in sql
    assert "operation = {p_operation:String}" in sql
    assert "success = {p_success:Bool}" in sql
    assert params["p_success"] is False


@pytest.mark.asyncio
async def test_fleet_cost_namespace_omits_namespaces_distinct() -> None:
    """When grouping by namespace, the redundant namespaces_distinct
    column shouldn't appear (group is already namespace)."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await admin_repo.fleet_cost(3600, 20, "namespace", ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "namespaces_distinct" not in sql


@pytest.mark.asyncio
async def test_fleet_cost_agent_includes_namespaces_distinct() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await admin_repo.fleet_cost(3600, 20, "agent", ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "uniqExact(namespace) AS namespaces_distinct" in sql


@pytest.mark.asyncio
async def test_budget_violations_org_filter() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await admin_repo.budget_violations(600, 50, org="clickhouse", ch=ch)
    sql, params = ch.query_all_calls[0]
    # Violations are `budget_violation` events on loom.events
    assert "kind = 'budget_violation'" in sql and "FROM loom.events" in sql
    assert params["org"] == "clickhouse"
