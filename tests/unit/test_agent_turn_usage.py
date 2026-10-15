"""cost.record_turns: the server prices the agent's raw usage counters."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.observability.cost_accumulator import is_priced, turn_cost_usd


def test_turn_cost_applies_cache_multipliers_and_the_fable_read_override() -> None:
    # opus 5: $5 / $25 per M -> 0.005 / 0.025 per 1k
    c = turn_cost_usd(
        "claude-opus-5",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_tokens=1000,
        cache_creation_5m_tokens=1000,
        cache_creation_1h_tokens=1000,
    )
    assert c == pytest.approx(0.005 + 0.025 + 0.0005 + 0.00625 + 0.010)
    # fable 5.1 reads at a flat $0.25/M, not 0.1 x $10
    f = turn_cost_usd(
        "claude-fable-5-1", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    )
    assert f == pytest.approx(0.25)
    assert turn_cost_usd("gpt-9-unknown", input_tokens=10**6, output_tokens=10**6) == 0.0
    assert is_priced("claude-sonnet-5") and not is_priced("gpt-9-unknown")


class _FakeCH:
    def __init__(self) -> None:
        self.rows: list[list[Any]] = []
        self.cols: list[str] = []

    async def insert(self, table, rows, *, column_names, settings=None):
        assert table == "loom.agent_turn_usage"
        self.rows.extend(rows)
        self.cols = column_names


def test_handler_prices_rows_and_reports_unpriced_models() -> None:
    from server.insights.api.models_cost import CostRecordTurnsInput
    from server.insights.app import cost as h

    fake = _FakeCH()

    class _Client:
        client = fake

    inp = CostRecordTurnsInput(
        org="o",
        namespace="n",
        agent="claude",
        session_id="s1",
        consumer="claude-code",
        turns=[
            {
                "message_id": "m1",
                "model": "claude-fable-5-1",
                "input_tokens": 2,
                "cache_read_tokens": 29592,
                "cache_creation_1h_tokens": 16146,
                "output_tokens": 292,
                "turn_at": "2026-09-11T17:00:00.000Z",
            },
            {
                "message_id": "m2",
                "model": "mystery-model",
                "input_tokens": 100,
                "output_tokens": 10,
            },
        ],
    )
    with patch.object(h, "get_client", return_value=_Client()):
        out = asyncio.run(h.handle_cost_record_turns(inp))
    assert out.recorded == 2 and out.priced == 1 and out.unpriced_models == ["mystery-model"]
    expected = (
        2 / 1000 * 0.010 + 29592 / 1000 * 0.00025 + 16146 / 1000 * 0.010 * 2.0 + 292 / 1000 * 0.050
    )
    assert out.cost_usd == pytest.approx(round(expected, 6))
    by = dict(zip(fake.cols, fake.rows[0], strict=True))
    assert by["session_id"] == "s1" and by["message_id"] == "m1" and by["priced"] is True
    assert by["cache_creation_1h_tokens"] == 16146 and by["turn_at"].year == 2026
    by2 = dict(zip(fake.cols, fake.rows[1], strict=True))
    assert by2["priced"] is False and by2["cost_usd"] == 0.0


def test_a_turn_dated_in_the_future_is_clamped_to_the_servers_clock() -> None:
    from datetime import UTC, datetime, timedelta

    from server.insights.api.models_cost import CostRecordTurnsInput
    from server.insights.app import cost as h

    fake = _FakeCH()

    class _Client:
        client = fake

    ahead = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    inp = CostRecordTurnsInput(
        org="o",
        namespace="n",
        session_id="s1",
        turns=[{"message_id": "m1", "model": "claude-opus-5-5", "turn_at": ahead}],
    )
    with patch.object(h, "get_client", return_value=_Client()):
        asyncio.run(h.handle_cost_record_turns(inp))
    by = dict(zip(fake.cols, fake.rows[0], strict=True))
    assert by["turn_at"] <= datetime.now(UTC) + h.TURN_AT_SLACK


def test_handler_write_failure_records_nothing() -> None:
    from server.insights.api.models_cost import CostRecordTurnsInput
    from server.insights.app import cost as h

    class _Boom:
        async def insert(self, *a, **k):
            raise RuntimeError("ch down")

    class _Client:
        client = _Boom()

    inp = CostRecordTurnsInput(
        org="o",
        namespace="n",
        session_id="s1",
        turns=[{"message_id": "m", "model": "claude-opus-5", "input_tokens": 1}],
    )
    with patch.object(h, "get_client", return_value=_Client()):
        out = asyncio.run(h.handle_cost_record_turns(inp))
    assert out.recorded == 0


def test_agent_summary_groups_and_totals_from_the_ledger() -> None:
    from datetime import date

    from server.insights.api.models_cost import CostAgentSummaryInput
    from server.insights.app import cost as h

    captured: dict[str, Any] = {}

    class _CH:
        async def query_all(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return [
                {
                    "model": "claude-fable-5-1",
                    "turns": 12,
                    "sessions": 3,
                    "input_tokens": 100,
                    "cache_read_tokens": 90000,
                    "cache_write_tokens": 16146,
                    "output_tokens": 2000,
                    "cost_usd": 0.445,
                    "unpriced_turns": 0,
                },
                {
                    "model": "mystery",
                    "turns": 2,
                    "sessions": 1,
                    "input_tokens": 10,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 5,
                    "cost_usd": 0.0,
                    "unpriced_turns": 2,
                },
            ]

    with patch.object(h, "get_client", return_value=_CH()):
        out = asyncio.run(
            h.handle_cost_agent_summary(
                CostAgentSummaryInput(
                    org="o",
                    namespace="n",
                    from_date=date(2026, 9, 1),
                    to_date=date(2026, 9, 11),
                    consumer="claude-code",
                )
            )
        )
    assert "FROM loom.agent_turn_usage FINAL" in captured["sql"]
    assert "GROUP BY model" in captured["sql"] and "consumer = {consumer:String}" in captured["sql"]
    assert captured["params"]["ns"] == "n" and captured["params"]["consumer"] == "claude-code"
    assert [r.model for r in out.rows] == ["claude-fable-5-1", "mystery"]
    assert out.total_turns == 14 and out.unpriced_turns == 2
    assert out.total_cost_usd == pytest.approx(0.445)
    assert out.rows[0].cache_write_tokens == 16146 and out.rows[0].namespace is None
