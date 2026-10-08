"""Every ledger row says which build wrote it."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from server.observability import audit, events
from tests.unit.schema_ddl import ddl


class _CH:
    def __init__(self) -> None:
        self.rows: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        self.rows.append((table, rows))


@pytest.mark.asyncio
async def test_audit_row_carries_the_build_and_the_hash_ignores_it(monkeypatch) -> None:
    from server.settings import settings

    ch = _CH()
    monkeypatch.setattr(audit, "get_client", lambda: ch)
    monkeypatch.setattr(settings, "build_commit", "abc1234")
    ts = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    rh = await audit.emit(
        "budget.violated",
        org="o",
        namespace="ns",
        actor="a",
        subject={"x": 1},
        request_id="r1",
        ts=ts,
    )
    ((table, rows),) = ch.rows
    row = rows[0]
    assert table == "loom.audit_log" and row["build_commit"] == "abc1234" and row["row_hash"] == rh
    # The hash payload is the seven original columns; the stamp is outside it,
    # so row_matches_hash and the checkpoint chain do not see it.
    canonical = {
        "ts": audit._ts_canonical_form(audit._normalize_ts(ts)),
        "event_type": "budget.violated",
        "org": "o",
        "namespace": "ns",
        "actor": "a",
        "subject": json.dumps({"x": 1}, sort_keys=True, default=str),
        "request_id": "r1",
    }
    assert rh == hashlib.sha256(audit._canonical(canonical)).hexdigest()
    monkeypatch.setattr(settings, "build_commit", "")
    await audit.emit(
        "budget.violated", org="o", namespace="ns", actor="a", subject={}, request_id="r2", ts=ts
    )
    assert ch.rows[1][1][0]["build_commit"] == ""  # unknown build stamps ''


def test_agent_event_row_carries_the_build(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "build_commit", "abc1234")
    e = events.AgentEvent(
        ts=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        org="o",
        namespace="ns",
        agent="a",
        user_id="u",
        session_id="s",
        operation="memory.set",
        request_id="r",
        latency_ms=5,
        success=True,
    )
    assert e.to_row()["build_commit"] == "abc1234"
    monkeypatch.setattr(settings, "build_commit", "")
    assert e.to_row()["build_commit"] == ""


def test_both_ledgers_carry_the_column() -> None:
    for table in ("audit_log", "agent_events"):
        assert "`build_commit` LowCardinality(String) DEFAULT ''" in ddl(table), table
