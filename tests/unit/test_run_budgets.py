"""budgets_exceeded: a budget of 0 is no budget, a run is over a budget only once
its spend passes it, and cost is listed before time."""

from __future__ import annotations

import pytest

from server.tasks.domain.budgets import BudgetKind, budgets_exceeded


@pytest.mark.parametrize(
    ("cost_usd", "elapsed_seconds", "over"),
    [
        (0.5, 30, []),
        (1.5, 30, ["cost"]),
        (0.5, 90, ["time"]),
        (1.5, 90, ["cost", "time"]),
    ],
)
def test_the_budgets_a_run_passed_in_order(
    cost_usd: float, elapsed_seconds: int, over: list[BudgetKind]
) -> None:
    got = budgets_exceeded(
        budget_usd=1.0, budget_seconds=60, cost_usd=cost_usd, elapsed_seconds=elapsed_seconds
    )
    assert got == over


def test_spend_exactly_at_the_budget_is_not_over() -> None:
    got = budgets_exceeded(budget_usd=1.0, budget_seconds=60, cost_usd=1.0, elapsed_seconds=60)
    assert got == []


@pytest.mark.parametrize(
    ("budget_usd", "budget_seconds", "over"),
    [
        (0.0, 0, []),
        (0.0, 60, ["time"]),
        (1.0, 0, ["cost"]),
    ],
)
def test_a_zero_budget_never_trips_even_with_spend(
    budget_usd: float, budget_seconds: int, over: list[BudgetKind]
) -> None:
    got = budgets_exceeded(
        budget_usd=budget_usd,
        budget_seconds=budget_seconds,
        cost_usd=1_000.0,
        elapsed_seconds=86_400,
    )
    assert got == over
