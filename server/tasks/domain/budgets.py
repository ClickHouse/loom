"""Run budgets: which of a run's budgets it has gone past."""

from __future__ import annotations

from typing import Literal

BudgetKind = Literal["cost", "time"]


def budgets_exceeded(
    budget_usd: float, budget_seconds: int, cost_usd: float, elapsed_seconds: int
) -> list[BudgetKind]:
    """The budgets a run has gone past; a budget of 0 is no budget."""
    over: list[BudgetKind] = []
    if budget_usd > 0 and cost_usd > budget_usd:
        over.append("cost")
    if budget_seconds > 0 and elapsed_seconds > budget_seconds:
        over.append("time")
    return over
