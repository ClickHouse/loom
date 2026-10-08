"""A budget violation is a `budget_violation` event on
loom.events, not an audit_log row; the admin reader reads the stream."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.insights.store import admin as admin_repo
from server.observability import events_stream as es
from server.settings import settings
from server.transport import _common
from tests.unit.test_memory_search_service import StubCH


def _reset() -> None:
    _common._BUDGET_LAST_EMIT.clear()
    _common._BUDGET_WINDOW_ROWS.clear()
    _common._BUDGET_SUPPRESSED.clear()
    es._queue.clear()
    if es._drain_task is not None:
        es._drain_task.cancel()
        es._drain_task = None


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]], dict[str, Any] | None]] = []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.calls.append((table, rows, settings))


@pytest.mark.asyncio
async def test_budget_violation_is_an_event_on_the_stream_not_an_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import audit

    fake = _FakeClient()
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    monkeypatch.setattr(audit, "get_client", lambda: fake)
    audit_rows: list[dict[str, Any]] = []

    async def _audit_emit(**kw: Any) -> str:
        audit_rows.append(kw)
        return "x" * 64

    monkeypatch.setattr(audit, "emit", _audit_emit)
    # the violation travels as an event; nothing reaches audit_log
    monkeypatch.setattr(settings, "events_stream_flush_interval_s", 60.0)
    monkeypatch.setattr(settings, "budget_violation_audit_interval_s", 60.0)
    monkeypatch.setattr(settings, "budget_violation_audit_burst", 1)
    _reset()
    for _ in range(4):
        await _common.emit_budget_violation(
            op_name="memory.set",
            request_id="r1",
            org="o",
            namespace="n",
            agent="a",
            elapsed_ms=900,
            budget_ms=600,
            transport="http",
        )
    assert audit_rows == [], "a budget violation is not an audit row"
    assert es.queued() == 1
    _common._BUDGET_LAST_EMIT["memory.set\x1fo\x1fn"] -= 61
    await _common.emit_budget_violation(
        op_name="memory.set",
        request_id="r2",
        org="o",
        namespace="n",
        agent="a",
        elapsed_ms=1200,
        budget_ms=600,
    )
    n = await es.flush_now()
    assert n == 2 and fake.calls[0][0] == "loom.events"
    first, second = fake.calls[0][1]
    assert first["kind"] == es.BUDGET_VIOLATION_KIND == "budget_violation"
    assert (
        first["org"],
        first["namespace"],
        first["agent"],
        first["actor"],
        first["request_id"],
    ) == (
        "o",
        "n",
        "a",
        "control-plane",
        "r1",
    )
    p1, p2 = json.loads(first["payload"]), json.loads(second["payload"])
    assert p1["event_type"] == "budget.violated" and p1["operation"] == "memory.set"
    assert p1["elapsed_ms"] == 900 and p1["budget_ms"] == 600 and p1["overage_ratio"] == 1.5
    assert p1["transport"] == "http" and "suppressed_since_last" not in p1
    assert (
        p2["suppressed_since_last"] == 3 and p2["overage_ratio"] == 2.0
    )  # the suppressed count rides the event
    assert "build_commit" in first  # the build stamp, typed
    _reset()


def test_the_stream_carries_budget_violations_only() -> None:
    assert es.KINDS == {es.BUDGET_VIOLATION_KIND}


@pytest.mark.asyncio
async def test_admin_reader_reads_the_stream_by_kind() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await admin_repo.budget_violations(600, 50, org="clickhouse", ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "FROM loom.events" in sql and "kind = 'budget_violation'" in sql
    assert "audit_log" not in sql and "JSONExtractString(payload, 'operation')" in sql
    assert "JSONExtractUInt(payload, 'suppressed_since_last')" in sql
    assert params["org"] == "clickhouse" and params["since"] == 600 and params["lim"] == 50
    assert "AND org = {org:String}" in sql
    ch2 = StubCH()
    ch2.query_all_returns.append([])
    await admin_repo.budget_violations(600, 50, ch=ch2)
    assert "{org:String}" not in ch2.query_all_calls[0][0]


def test_an_http_violation_row_names_its_transport_as_mcp_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both transports tag the row, so a by-transport read of the SLO stream
    does not file every HTTP violation under an empty transport."""
    import dataclasses

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import http as http_mod

    seen: list[dict[str, Any]] = []

    async def _row(**kw: Any) -> None:
        seen.append(kw)

    base = OPERATIONS_BY_NAME["memory.unlock"]

    async def _handler(inp: Any) -> Any:
        return base.output_model.model_construct(unlocked=True)

    op = dataclasses.replace(base, handler=_handler, audit=False)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr("server.transport._common.emit_budget_violation", _row)
    monkeypatch.setattr("server.observability.budgets.effective_budget_ms", lambda name, budget: -1)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    body = {"org": "o", "namespace": "n", "agent": "a", "memory_key": "k", "reason": "t"}
    r = TestClient(app).post("/v1/memory.unlock", json=body)
    assert r.status_code == 200, r.text
    assert [r["transport"] for r in seen] == ["http"]
