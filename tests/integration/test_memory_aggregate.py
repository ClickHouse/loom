"""memory.aggregate — deterministic SQL aggregation over typed records.

Pairs with `set_from_messages(record_schema=...)` and `bulk_set` writes
whose `value` is a JSON object. Vector search can't answer "how many"
or "sum by category" — that needs predicate truth, which compression
into an embedding can't preserve. memory.aggregate plugs that gap.

The SDK surface exposes:
  - `set_from_messages(record_schema={"field": "type"|[enum_vals]},
                        memory_key="...")` for typed extraction.
  - `aggregate(agg_func=, agg_field=, group_by=, filter=, limit=)`
    for the read path.

These tests use bulk_set with dict-shaped values to seed typed records
directly — avoids the LLM dependency that set_from_messages would add.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.fixture
async def seeded_expenses(agent):
    """Seed a small set of typed expense records under the agent's
    identity. Used by the aggregate-shape tests below."""
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "exp_1",
                "value": {"category": "food", "amount": 12.50},
                "memory_type": "semantic",
            },
            {
                "memory_key": "exp_2",
                "value": {"category": "food", "amount": 8.75},
                "memory_type": "semantic",
            },
            {
                "memory_key": "exp_3",
                "value": {"category": "transit", "amount": 3.25},
                "memory_type": "semantic",
            },
            {
                "memory_key": "exp_4",
                "value": {"category": "food", "amount": 22.00},
                "memory_type": "semantic",
            },
            {
                "memory_key": "exp_5",
                "value": {"category": "entertainment", "amount": 50.00},
                "memory_type": "semantic",
            },
        ]
    )
    return agent


@pytest.mark.asyncio
async def test_aggregate_count_total(agent) -> None:
    """count(*) with no group_by returns one row with the total."""
    await agent.memory.bulk_set(
        [{"memory_key": f"k_{i}", "value": {"n": i}, "memory_type": "semantic"} for i in range(7)]
    )
    out = await agent.memory.aggregate(agg_func="count")
    assert out["rows"]
    # No group_by → one row, empty group dict, metric = total count
    assert len(out["rows"]) == 1
    assert out["rows"][0]["group"] == {}
    assert int(out["rows"][0]["metric"]) == 7


@pytest.mark.asyncio
async def test_aggregate_sum_by_group(seeded_expenses) -> None:
    """sum(amount) GROUP BY category — exact arithmetic over typed values."""
    out = await seeded_expenses.memory.aggregate(
        agg_func="sum",
        agg_field="amount",
        group_by=["category"],
    )
    rows = {r["group"]["category"]: r["metric"] for r in out["rows"]}
    assert rows["food"] == pytest.approx(43.25)
    assert rows["transit"] == pytest.approx(3.25)
    assert rows["entertainment"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_aggregate_avg_by_group(seeded_expenses) -> None:
    """avg(amount) GROUP BY category — average expense per category."""
    out = await seeded_expenses.memory.aggregate(
        agg_func="avg",
        agg_field="amount",
        group_by=["category"],
    )
    rows = {r["group"]["category"]: r["metric"] for r in out["rows"]}
    # food has 12.50 + 8.75 + 22.00 = 43.25 across 3 → avg 14.4166...
    assert rows["food"] == pytest.approx(14.4166, abs=0.01)


@pytest.mark.asyncio
async def test_aggregate_count_by_group(seeded_expenses) -> None:
    """count() GROUP BY category — how many of each."""
    out = await seeded_expenses.memory.aggregate(
        agg_func="count",
        group_by=["category"],
    )
    counts = {r["group"]["category"]: int(r["metric"]) for r in out["rows"]}
    assert counts["food"] == 3
    assert counts["transit"] == 1
    assert counts["entertainment"] == 1


@pytest.mark.asyncio
async def test_aggregate_filter_narrows_rows(seeded_expenses) -> None:
    """filter={field: value} restricts the candidate set before aggregating."""
    out = await seeded_expenses.memory.aggregate(
        agg_func="sum",
        agg_field="amount",
        filter={"category": "food"},
    )
    # Only food rows survive the filter — total should be just the food sum
    assert len(out["rows"]) == 1
    assert out["rows"][0]["metric"] == pytest.approx(43.25)


@pytest.mark.asyncio
async def test_aggregate_min_max(seeded_expenses) -> None:
    """min and max are also valid agg_funcs."""
    out_min = await seeded_expenses.memory.aggregate(agg_func="min", agg_field="amount")
    out_max = await seeded_expenses.memory.aggregate(agg_func="max", agg_field="amount")
    assert out_min["rows"][0]["metric"] == pytest.approx(3.25)
    assert out_max["rows"][0]["metric"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_aggregate_excludes_free_text_rows(agent) -> None:
    """Rows whose `value` isn't JSON-shaped don't break the aggregate —
    they're silently excluded (JSONExtract returns empty / 0)."""
    # One typed row + one free-text row mixed in
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "typed_1",
                "value": {"category": "food", "amount": 10.0},
                "memory_type": "semantic",
            },
            {
                "memory_key": "free_text",
                "value": "User prefers dark mode",  # not JSON object
                "memory_type": "preference",
            },
        ]
    )
    out = await agent.memory.aggregate(
        agg_func="sum",
        agg_field="amount",
        group_by=["category"],
    )
    # Only the typed row contributes — free-text row excluded
    rows = {r["group"]["category"]: r["metric"] for r in out["rows"]}
    assert rows.get("food") == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_set_from_messages_requires_memory_key_with_schema(agent) -> None:
    """SDK guards: record_schema + missing memory_key → ValueError before
    the HTTP call. Prevents wasting an LLM token on a request the server
    would reject."""
    with pytest.raises(ValueError, match="memory_key is required"):
        await agent.memory.set_from_messages(
            [{"role": "user", "content": "I spent $12 on coffee today."}],
            record_schema={
                "amount": "number",
                "category": ["food", "transit", "entertainment"],
            },
            # memory_key intentionally omitted
        )


@pytest.mark.asyncio
async def test_aggregate_returns_sql_executed(seeded_expenses) -> None:
    """The response carries sql_executed for debugging — callers shouldn't
    have to turn on trace logging to see what query ran."""
    out = await seeded_expenses.memory.aggregate(
        agg_func="count",
        group_by=["category"],
    )
    assert "sql_executed" in out
    assert out["sql_executed"]  # non-empty
    # Sanity: the SQL should mention the namespace and the aggregate function
    sql = out["sql_executed"].lower()
    assert "count" in sql
