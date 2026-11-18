"""A composition seam for optional, check-time budget warnings."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetWarning:
    org: str
    namespace: str
    inbox: str
    user_id: str
    cap: float
    spent: float
    share: float
    observed_at: float


_observer: Callable[[BudgetWarning], None] | None = None


def use_observer(observer: Callable[[BudgetWarning], None]) -> None:
    global _observer
    _observer = observer


def notify(warning: BudgetWarning) -> None:
    if _observer is None:
        return
    try:
        _observer(warning)
    except Exception as e:  # noqa: BLE001 — notification failure cannot change admission
        log.warning("budget warning could not be scheduled: %s", e)


def writer_lease_name(org: str) -> str:
    return f"budget.warning.writer/{org}"
