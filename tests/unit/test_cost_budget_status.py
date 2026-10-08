"""cost.budget_status for one namespace: the cap comes from the same rule the
worker budget guard applies (the namespace's own cap, else the server
default, else uncapped), with the 24 h spend read either way."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.insights.api.models_cost import BudgetStatusInput
from server.insights.app import cost


class _CH:
    def __init__(self, cap: float | None, spent: float) -> None:
        self.cap, self.spent = cap, spent

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        assert params == {"org": "o", "ns": "n"}
        if "daily_cost_cap_usd" in sql:
            return None if self.cap is None else {"daily_cost_cap_usd": self.cap}
        return {"spent": self.spent}


def _status(monkeypatch: pytest.MonkeyPatch, ch: _CH, default_cap: float) -> Any:
    from server.settings import settings

    monkeypatch.setattr(cost, "get_client", lambda: ch)
    monkeypatch.setattr(settings, "default_daily_cost_cap_usd", default_cap)
    return asyncio.run(cost.handle_budget_status(BudgetStatusInput(org="o", namespace="n")))


def test_the_namespace_cap_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _status(monkeypatch, _CH(cap=2.0, spent=3.0), default_cap=1.0)
    assert (out.cap_source, out.cap_usd, out.spent_24h_usd) == ("namespace", 2.0, 3.0)
    assert out.over_cap is True and out.pct_of_cap == 1.0 and out.namespace == "n"


def test_no_namespace_cap_takes_the_server_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for cap in (None, 0.0):
        out = _status(monkeypatch, _CH(cap=cap, spent=0.25), default_cap=1.0)
        assert (out.cap_source, out.cap_usd) == ("global", 1.0)
        assert out.over_cap is False and out.pct_of_cap == 0.25


def test_zero_on_both_tiers_is_uncapped_and_still_reports_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _status(monkeypatch, _CH(cap=0.0, spent=7.5), default_cap=0.0)
    assert (out.cap_source, out.cap_usd, out.spent_24h_usd) == ("uncapped", 0.0, 7.5)
    assert out.over_cap is False and out.pct_of_cap is None
