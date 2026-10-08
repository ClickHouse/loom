"""The request shape a handler declares for its latency budget.

An operation's `budget_p99_ms` prices its default shape. Some requests are a
different shape of work under the same name — a `code.symbol` lookup ranked by
the change at hand reads the PR's files and every candidate's neighbourhood
where the bare lookup reads one row — and holding both to one number either
flags every ranked call or hides a slow bare one. The handler notes the shape
it served; the transport prices the request by
`Operation.shape_budgets_p99_ms[shape]` when the operation declares it, else
by the base budget. Request-scoped, like the ledger run and the consumer.
"""

from __future__ import annotations

from contextvars import ContextVar

_SHAPE_MAX = 32

budget_shape_var: ContextVar[str] = ContextVar("loom_budget_shape", default="")


def note_budget_shape(shape: object) -> None:
    """Declare the shape this request served ('' clears). Never raises."""
    try:
        budget_shape_var.set(str(shape or "").strip()[:_SHAPE_MAX])
    except Exception:  # noqa: BLE001 — telemetry never fails a request
        pass


def current_budget_shape() -> str:
    return budget_shape_var.get()


__all__ = ["budget_shape_var", "current_budget_shape", "note_budget_shape"]
