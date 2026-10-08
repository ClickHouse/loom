"""The single-namespace cost-cap reads: `namespace_over_cap` and
`namespace_budget_ratio` answer from the same cap and 24 h spend."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.observability import budget


class _CH:
    def __init__(self, cap: float | None, spent: float, *, boom: bool = False) -> None:
        self.cap, self.spent, self.boom = cap, spent, boom
        self.statements: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if self.boom:
            raise RuntimeError("ch down")
        self.statements.append(sql)
        if "daily_cost_cap_usd" in sql:
            return None if self.cap is None else {"daily_cost_cap_usd": self.cap}
        return {"spent": self.spent}


def _read(monkeypatch: pytest.MonkeyPatch, ch: _CH, default_cap: float) -> tuple[bool, float]:
    from server.settings import settings

    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(settings, "default_daily_cost_cap_usd", default_cap)
    over = asyncio.run(budget.namespace_over_cap("o", "n"))
    ratio = asyncio.run(budget.namespace_budget_ratio("o", "n"))
    return over, ratio


def test_uncapped_reads_no_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(cap=0.0, spent=99.0)
    assert _read(monkeypatch, ch, default_cap=0.0) == (False, 0.0)
    assert not any("agent_events" in s for s in ch.statements)


def test_namespace_cap_wins_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _read(monkeypatch, _CH(cap=2.0, spent=1.0), default_cap=100.0) == (False, 0.5)
    assert _read(monkeypatch, _CH(cap=2.0, spent=2.0), default_cap=100.0) == (True, 1.0)


def test_default_cap_applies_without_a_namespace_row(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _read(monkeypatch, _CH(cap=None, spent=3.0), default_cap=2.0) == (True, 1.5)


def test_a_read_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _read(monkeypatch, _CH(cap=1.0, spent=5.0, boom=True), default_cap=1.0) == (False, 0.0)
