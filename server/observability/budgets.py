"""Per-operation and per-consumer latency-budget overrides.

Every Operation declares a `budget_p99_ms` — the p99 latency target.
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
    5. the op's declared budget_p99_ms

A value is an integer (milliseconds) or a string multiplier such as "3x"
(the op's declared budget times three), so one entry loosens every op for a
consumer without listing them. The transport then applies the deployment's
`latency_budget_multiplier` to whatever this resolves.

The runtime table binds every web process of one server: with more than one
(`LOOM_WEB_WORKERS`) it lives in a host file beside the shared rate-limit
table, re-read on change and updated under a lock. It is not shared across
replicas; the settings map is the fleet-wide half.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

ANY_OP = "*"


def _parse_value(raw: Any) -> int | float | None:
    """An override entry: int/float milliseconds, or "<n>x" multiplier
    returned as a float factor. None when unusable."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return int(raw) if raw >= 0 else None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text.endswith("x"):
            try:
                factor = float(text[:-1])
            except ValueError:
                return None
            return factor if factor >= 0 else None
        try:
            return int(float(text)) if float(text) >= 0 else None
        except ValueError:
            return None
    return None


def _apply(value: int | float, default_ms: int) -> int:
    """A float is a multiplier of the op's declared budget; an int is ms."""
    if isinstance(value, float):
        return int(round(default_ms * value))
    return int(value)


class BudgetOverrides:
    """Singleton overrides registry: op_name → budget_ms, plus
    (consumer, op_name | "*") → budget_ms or multiplier."""

    def __init__(self) -> None:
        self._overrides: dict[str, int] = {}
        self._by_consumer: dict[tuple[str, str], int | float] = {}
        self._lock = threading.Lock()
        # The settings map, parsed once per distinct raw string.
        self._settings_raw: str | None = None
        self._settings_map: dict[str, dict[str, int | float]] = {}
        # The host file's identity when last read or written; None = not yet.
        self._file_sig: tuple[int, int, int] | None = None
        # Tests point this at a file; None = derive from settings.
        self.path: str | None = None

    # -- the host file the web processes share -----------------------------

    def _file(self) -> str | None:
        if self.path is not None:
            return self.path
        from server.settings import settings

        if int(settings.web_workers) <= 1:
            return None
        from server.core.ratelimit_shared import runtime_dir

        return os.path.join(runtime_dir(), f"loom-budgets-{os.getppid()}.json")

    def _sync(self, *, strict: bool = False) -> None:
        """Adopt the host file when it changed (caller holds `_lock`). An
        unreadable file keeps the cached state, unless `strict`: an update must
        not publish over state it cannot read."""
        path = self._file()
        if path is None:
            return
        try:
            st = os.stat(path)
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            if sig == self._file_sig:
                return
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            ops = {str(k): int(v) for k, v in data["ops"].items()}
            by_consumer = {(str(c), str(o)): v for c, o, v in data["consumer"]}
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as e:
            if strict:
                raise OSError(f"{path} unreadable: {e}") from e
            log.warning("budget overrides: %s unreadable (%s); keeping mine", path, e)
            return
        self._overrides, self._by_consumer, self._file_sig = ops, by_consumer, sig

    @contextlib.contextmanager
    def _update(self) -> Iterator[None]:
        """Mutate under `_lock` and the host file's lock: read, change, publish.
        A change that cannot be published is rolled back and refused."""
        with self._lock:
            path = self._file()
            if path is None:
                yield
                return
            from server.core.errors import LoomServerError
            from server.core.ratelimit_shared import file_lock, publish_json

            try:
                with file_lock(path):
                    self._sync(strict=True)
                    before = (dict(self._overrides), dict(self._by_consumer))
                    yield
                    data = {
                        "ops": self._overrides,
                        "consumer": [[c, o, v] for (c, o), v in self._by_consumer.items()],
                    }
                    try:
                        self._file_sig = publish_json(path, data)
                    except OSError:
                        self._overrides, self._by_consumer = before
                        raise
            except OSError as e:
                raise LoomServerError(
                    f"budget override not applied: {path} could not be updated ({e})"
                ) from e

    # -- per op (unchanged API) -------------------------------------------

    def set(self, op_name: str, budget_ms: int) -> None:
        if budget_ms < 0:
            raise ValueError("budget_ms must be >= 0")
        with self._update():
            self._overrides[op_name] = budget_ms

    def get(self, op_name: str) -> int | None:
        with self._lock:
            self._sync()
            return self._overrides.get(op_name)

    def clear(self, op_name: str) -> None:
        with self._update():
            self._overrides.pop(op_name, None)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            self._sync()
            return dict(self._overrides)

    # -- per consumer --------------------------------------

    def set_for_consumer(self, consumer: str, op_name: str, value: int | str) -> None:
        """`value` is milliseconds or a "<n>x" multiplier; `op_name` "*"
        covers every op for the consumer."""
        if not consumer:
            raise ValueError("consumer must be non-empty")
        parsed = _parse_value(value)
        if parsed is None:
            raise ValueError("budget must be a non-negative integer (ms) or '<n>x'")
        with self._update():
            self._by_consumer[(consumer, op_name or ANY_OP)] = parsed

    def get_for_consumer(self, consumer: str, op_name: str) -> int | float | None:
        with self._lock:
            self._sync()
            hit = self._by_consumer.get((consumer, op_name))
            if hit is None:
                hit = self._by_consumer.get((consumer, ANY_OP))
            return hit

    def clear_for_consumer(self, consumer: str, op_name: str = ANY_OP) -> None:
        with self._update():
            self._by_consumer.pop((consumer, op_name or ANY_OP), None)

    def snapshot_by_consumer(self) -> dict[str, dict[str, int | float]]:
        with self._lock:
            self._sync()
            out: dict[str, dict[str, int | float]] = {}
            for (consumer, op_name), value in self._by_consumer.items():
                out.setdefault(consumer, {})[op_name] = value
            return out

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
                            v = _parse_value(value)
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
        return int(default_ms)


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


__all__ = [
    "ANY_OP",
    "BudgetOverrides",
    "declared_budget_ms",
    "effective_budget_ms",
    "get_overrides",
]
