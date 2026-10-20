"""Per-operation and per-consumer latency-budget overrides.

Every Operation has a `budget_p99_ms` — the p99 latency target, from the
`OP_BUDGETS_P99_MS` table in `server/settings/catalog.py`.
When a handler exceeds it, the transport emits a `budget.violated` event
on `loom.events` (kind `budget_violation`) so ops can alert
on SLO breaches through `memory.budget_violations`.

This module lets you tighten or loosen a budget without a redeploy.
Production use: a new handler's first day in prod gets a loose budget
while you measure; once you've calibrated, you tighten via
`POST /v1/admin/budget.set`. Test use: set budget=0 to force violations
deterministically.

Budgets also have a consumer axis: loom serves several kinds of consumer with different
shapes of traffic — a bench harness that batches 30k writes should not page the same alert
as a production plugin's `memory.search`. The effective budget for a request is resolved in
this order, first match wins:

    1. runtime override for (consumer, op)        — admin budget.set {consumer, op}
    2. runtime override for (consumer, "*")       — admin budget.set {consumer, op: "*"}
    3. LOOM_LATENCY_BUDGET_OVERRIDES_BY_CONSUMER   — JSON {consumer: {op | "*": value}}
    4. runtime override for the op                 — admin budget.set {op}
    5. LOOM_LATENCY_BUDGET_OVERRIDES               — JSON {op: value}
    6. the op's declared budget_p99_ms

A value is an integer (milliseconds) or a string multiplier such as "3x"
(the op's declared budget times three), so one entry loosens every op for a
consumer without listing them. The transport then applies the deployment's
`latency_budget_multiplier` to whatever this resolves.

The runtime overrides are the deployment's: the admin route writes them to
loom.runtime_overrides and every process adopts that table on a timer
(`adopt`).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from server.core.numbers import finite_non_negative, parse_budget_value

log = logging.getLogger(__name__)

ANY_OP = "*"


def _apply(value: int | float, default_ms: int) -> int:
    """A float is a multiplier of the op's declared budget; an int is ms."""
    if isinstance(value, float):
        return int(round(default_ms * value))
    return int(value)


def stored_ms(raw: Any) -> int:
    """An op budget as `set` stores it: whole milliseconds, finite and >= 0."""
    if not isinstance(raw, int) or not finite_non_negative(raw):
        raise ValueError(f"budget_ms must be a whole number of ms >= 0, not {raw!r}")
    return raw


class BudgetOverrides:
    """Singleton overrides registry: op_name → budget_ms, plus
    (consumer, op_name | "*") → budget_ms or multiplier."""

    def __init__(self) -> None:
        self._overrides: dict[str, int] = {}
        self._by_consumer: dict[tuple[str, str], int | float] = {}
        self._lock = threading.Lock()
        # The settings maps, each parsed once per distinct raw string.
        self._settings_raw: str | None = None
        self._settings_map: dict[str, dict[str, int | float]] = {}
        self._op_settings_raw: str | None = None
        self._op_settings_map: dict[str, int | float] = {}

    # -- per op (unchanged API) -------------------------------------------

    def set(self, op_name: str, budget_ms: int) -> None:
        stored = stored_ms(budget_ms)
        with self._lock:
            self._overrides[op_name] = stored

    def get(self, op_name: str) -> int | None:
        with self._lock:
            return self._overrides.get(op_name)

    def clear(self, op_name: str) -> None:
        with self._lock:
            self._overrides.pop(op_name, None)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._overrides)

    # -- per consumer --------------------------------------

    def set_for_consumer(self, consumer: str, op_name: str, value: int | str) -> None:
        """`value` is milliseconds or a "<n>x" multiplier; `op_name` "*"
        covers every op for the consumer."""
        if not consumer:
            raise ValueError("consumer must be non-empty")
        parsed = parse_budget_value(value)
        if parsed is None:
            raise ValueError("budget must be a non-negative integer (ms) or '<n>x'")
        with self._lock:
            self._by_consumer[(consumer, op_name or ANY_OP)] = parsed

    def get_for_consumer(self, consumer: str, op_name: str) -> int | float | None:
        with self._lock:
            hit = self._by_consumer.get((consumer, op_name))
            if hit is None:
                hit = self._by_consumer.get((consumer, ANY_OP))
            return hit

    def clear_for_consumer(self, consumer: str, op_name: str = ANY_OP) -> None:
        with self._lock:
            self._by_consumer.pop((consumer, op_name or ANY_OP), None)

    def snapshot_by_consumer(self) -> dict[str, dict[str, int | float]]:
        with self._lock:
            out: dict[str, dict[str, int | float]] = {}
            for (consumer, op_name), value in self._by_consumer.items():
                out.setdefault(consumer, {})[op_name] = value
            return out

    def adopt(self, snapshot: dict[str, dict[tuple[str, ...], Any]]) -> None:
        """Take the deployment's overrides (a `runtime_overrides.load()` snapshot)."""
        from server.core import runtime_overrides as ro

        ops = {
            k[0]: v
            for k, v in snapshot.get(ro.BUDGET_OP, {}).items()
            if len(k) == 1 and isinstance(v, int) and not isinstance(v, bool) and v >= 0
        }
        consumers = {
            (k[0], k[1]): parsed
            for k, v in snapshot.get(ro.BUDGET_CONSUMER, {}).items()
            if len(k) == 2 and (parsed := parse_budget_value(v)) is not None
        }
        with self._lock:
            self._overrides, self._by_consumer = ops, consumers

    def settings_map(self) -> dict[str, dict[str, int | float]]:
        """`latency_budget_overrides_by_consumer` parsed: {consumer: {op: value}}.
        Re-parsed only when the raw string changes; a malformed map is
        logged once and treated as empty — a bad override must not take the
        budgets down with it."""
        from server.settings import settings

        raw = str(settings.latency_budget_overrides_by_consumer)
        with self._lock:
            if raw == self._settings_raw:
                return self._settings_map
            parsed: dict[str, dict[str, int | float]] = {}
            if raw.strip():
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict):
                        raise ValueError("not a JSON object")
                    for consumer, ops in obj.items():
                        if not isinstance(ops, dict):
                            raise ValueError(f"{consumer!r}: expected an object of op -> value")
                        entry: dict[str, int | float] = {}
                        for op_name, value in ops.items():
                            v = parse_budget_value(value)
                            if v is None:
                                raise ValueError(
                                    f"{consumer!r}.{op_name!r}: {value!r} is not ms or '<n>x'"
                                )
                            entry[str(op_name)] = v
                        parsed[str(consumer)] = entry
                except (ValueError, TypeError) as exc:
                    log.warning("LOOM_LATENCY_BUDGET_OVERRIDES_BY_CONSUMER ignored: %s", exc)
                    parsed = {}
            self._settings_raw = raw
            self._settings_map = parsed
            return parsed

    def effective(self, op_name: str, default_ms: int, *, consumer: str = "") -> int:
        """The budget a request is held to before the deployment multiplier,
        resolved in the order the module docstring gives."""
        if consumer:
            hit = self.get_for_consumer(consumer, op_name)
            if hit is not None:
                return _apply(hit, default_ms)
            entry = self.settings_map().get(consumer)
            if entry:
                value = entry.get(op_name)
                if value is None:
                    value = entry.get(ANY_OP)
                if value is not None:
                    return _apply(value, default_ms)
        op_override = self.get(op_name)
        if op_override is not None:
            return op_override
        value = self.op_settings_map().get(op_name)
        if value is not None:
            return _apply(value, default_ms)
        return int(default_ms)

    def op_settings_map(self) -> dict[str, int | float]:
        """`latency_budget_overrides` parsed: {op: value}, re-parsed only when the raw
        string changes. The field refuses an unusable budget at startup; an op this build
        lacks is logged once and ignored."""
        from server.settings import settings
        from server.settings.catalog import OP_BUDGETS_P99_MS, parse_latency_budgets

        raw = str(settings.latency_budget_overrides)
        with self._lock:
            if raw == self._op_settings_raw:
                return self._op_settings_map
            parsed = parse_latency_budgets(raw)
            unknown = sorted(set(parsed) - set(OP_BUDGETS_P99_MS))
            if unknown:
                log.warning(
                    "LOOM_LATENCY_BUDGET_OVERRIDES names no op of this build, ignored: %s", unknown
                )
            self._op_settings_raw = raw
            self._op_settings_map = parsed
            return parsed


_overrides = BudgetOverrides()


def get_overrides() -> BudgetOverrides:
    return _overrides


def declared_budget_ms(op_name: str, declared_ms: int) -> int:
    """The budget the operation declares for THIS request: the shape the
    handler noted (`server.observability.budget_shape`) when
    the operation prices it, else `declared_ms` (its `budget_p99_ms`). A
    shape the operation does not price falls back to the base budget, so a
    stray note is harmless; an unknown op name is the base budget too."""
    from server.observability.budget_shape import current_budget_shape

    shape = current_budget_shape()
    if not shape:
        return int(declared_ms)
    try:
        from server.operations.registry import OPERATIONS_BY_NAME

        op = OPERATIONS_BY_NAME.get(op_name)
        priced = getattr(op, "shape_budgets_p99_ms", None) or {}
        return int(priced.get(shape, declared_ms))
    except Exception:  # noqa: BLE001 — a budget lookup never fails a request
        return int(declared_ms)


def effective_budget_ms(op_name: str, declared_ms: int, *, consumer: str | None = None) -> int:
    """The budget a request is held to: the override registry's resolution
    (per consumer, then per op, then the declaration) scaled
    by the deployment's `latency_budget_multiplier`. Both transports call
    this so a consumer's budget is the same over HTTP and MCP."""
    from server.observability.consumer import current_consumer
    from server.settings import settings

    if consumer is None:
        consumer = current_consumer()
    # A handler-declared shape prices the request first.
    base = get_overrides().effective(
        op_name, declared_budget_ms(op_name, declared_ms), consumer=consumer
    )
    return int(base * settings.latency_budget_multiplier)


def request_time_left_s(op_name: str) -> float:
    """Seconds left of the bound request's latency budget for `op_name`: 0 once spent,
    `inf` with no request bound (a worker pass, a test) or no budget for the op."""
    import time

    from server.core import request_work

    bag = request_work.current()
    try:
        from server.operations.registry import OPERATIONS_BY_NAME

        op = OPERATIONS_BY_NAME.get(op_name)
        budget_ms = effective_budget_ms(op_name, op.budget_p99_ms) if op is not None else 0
    except Exception:  # noqa: BLE001 — a budget lookup never fails a request
        budget_ms = 0
    if bag is None or budget_ms <= 0:
        return float("inf")
    return max(0.0, budget_ms / 1000.0 - (time.perf_counter() - bag.began))


__all__ = [
    "ANY_OP",
    "BudgetOverrides",
    "declared_budget_ms",
    "effective_budget_ms",
    "get_overrides",
    "request_time_left_s",
]
