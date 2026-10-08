"""A budget violation is audited once per op per interval."""

from __future__ import annotations

from typing import Any

import pytest

from server.settings import settings
from server.transport import _common


def _reset() -> None:
    _common._BUDGET_LAST_EMIT.clear()
    _common._BUDGET_WINDOW_ROWS.clear()
    _common._BUDGET_SUPPRESSED.clear()


def test_gate_writes_a_burst_then_counts_then_carries_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "budget_violation_audit_interval_s", 60.0)
    monkeypatch.setattr(settings, "budget_violation_audit_burst", 3)
    _reset()
    g = _common._budget_violation_gate
    assert [g("memory.set", "o", "n", now=1000.0 + i) for i in range(3)] == [
        0,
        0,
        0,
    ]  # the burst writes
    assert g("memory.set", "o", "n", now=1010.0) is None  # beyond the burst: counted
    assert g("memory.set", "o", "n", now=1020.0) is None
    assert g("memory.get", "o", "n", now=1020.0) == 0  # another op has its own window
    assert g("memory.set", "o", "other", now=1020.0) == 0  # so does another namespace
    assert (
        g("memory.set", "o", "n", now=1061.0) == 2
    )  # the next window's first row carries the count
    assert g("memory.set", "o", "n", now=1062.0) == 0  # and its burst starts again


def test_interval_zero_writes_every_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "budget_violation_audit_interval_s", 0.0)
    _reset()
    assert [
        _common._budget_violation_gate("memory.set", "o", "n", now=float(t)) for t in range(3)
    ] == [0, 0, 0]


@pytest.mark.asyncio
async def test_emit_budget_violation_writes_one_row_with_the_suppressed_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The row is a `budget_violation` event on loom.events; the rate-limit
    # gate and its suppressed count are unchanged.
    from server.observability import events_stream

    rows: list[dict[str, Any]] = []

    def _record(kind: str, row: dict[str, Any]) -> dict[str, Any]:
        rows.append({"kind": kind, **row})
        return row

    monkeypatch.setattr(events_stream, "record", _record)
    monkeypatch.setattr(settings, "budget_violation_audit_interval_s", 60.0)
    monkeypatch.setattr(settings, "budget_violation_audit_burst", 1)
    _reset()
    for _ in range(5):
        await _common.emit_budget_violation(
            op_name="memory.set",
            request_id="r",
            org="o",
            namespace="n",
            agent="a",
            elapsed_ms=900,
            budget_ms=600,
        )
    assert len(rows) == 1 and rows[0]["event_type"] == "budget.violated"
    assert rows[0]["kind"] == "budget_violation" and "suppressed_since_last" not in rows[0]
    _common._BUDGET_LAST_EMIT["memory.set\x1fo\x1fn"] -= 61  # the window has passed
    await _common.emit_budget_violation(
        op_name="memory.set",
        request_id="r",
        org="o",
        namespace="n",
        agent="a",
        elapsed_ms=900,
        budget_ms=600,
    )
    assert len(rows) == 2 and rows[1]["suppressed_since_last"] == 4
    assert rows[1]["overage_ratio"] == 1.5
    _reset()


def test_memory_set_budget_names_its_real_shape() -> None:
    from server.memory.api.ops_crud import memory_set

    assert memory_set.budget_p99_ms == 200
