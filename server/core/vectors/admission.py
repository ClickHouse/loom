"""Weighted local admission for explicitly attributed vector statements."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from server.core.ch_errors import error_code
from server.core.errors import (
    VectorAdmissionError,
    VectorCapacityError,
    VectorCapacityReason,
    VectorOverloadedError,
)
from server.settings import settings


@dataclass(frozen=True)
class VectorWork:
    org: str
    namespaces: tuple[str, ...]
    weight: int


_work: ContextVar[VectorWork | None] = ContextVar("vector_statement_work", default=None)
_without_wait: ContextVar[bool] = ContextVar("vector_admission_without_wait", default=False)


@contextmanager
def _without_admission_wait() -> Iterator[None]:
    """Refuse capacity waits in completion-sensitive supplemental reads."""
    token = _without_wait.set(True)
    try:
        yield
    finally:
        _without_wait.reset(token)


@contextmanager
def vector_work(
    org: str, namespaces: str | tuple[str, ...] | list[str], weight: int = 1
) -> Iterator[None]:
    """Attribute only the actual vector dispatch; this scope holds no credit."""
    scope = (namespaces,) if isinstance(namespaces, str) else tuple(namespaces)
    if weight == 0:
        token = _work.set(None)
        try:
            yield
        finally:
            _work.reset(token)
        return
    if not org or not scope or not all(scope) or weight < 1:
        raise VectorAdmissionError("vector work has no valid target scope", reason="attribution")
    token = _work.set(VectorWork(org, scope, weight))
    try:
        yield
    finally:
        _work.reset(token)


def current_work() -> VectorWork | None:
    return _work.get()


def _limits() -> tuple[int, int]:
    c = int(settings.vector_scan_max_concurrent_per_org)
    w = int(settings.vector_scan_max_waiters_per_org)
    if c < 2 or w < 1:
        raise VectorAdmissionError(
            "vector admission is not configured safely", reason="configuration"
        )
    return c, w


def batch_size(configured: int) -> int:
    """A planned multi-probe statement fits the current weighted org ceiling."""
    c, _ = _limits()
    return min(max(1, configured), c)


@dataclass
class _Waiter:
    weight: int
    ready: asyncio.Future[None]
    granted: bool = False


@dataclass
class _Org:
    held: int = 0
    waiting: int = 0
    queue: deque[_Waiter] = field(default_factory=deque)


class _Admission:
    def __init__(self) -> None:
        self.population = 0
        self.orgs: dict[str, _Org] = {}

    def _advance(self, org: _Org) -> None:
        try:
            c, _ = _limits()
        except VectorCapacityError:
            # Invalid live configuration stops grants while old charges drain.
            return
        while org.queue:
            head = org.queue[0]
            if head.ready.done():
                org.queue.popleft()
                org.waiting -= head.weight
                continue
            if head.weight > c:
                org.queue.popleft()
                org.waiting -= head.weight
                head.ready.set_exception(
                    VectorAdmissionError(
                        "vector statement exceeds the org work ceiling",
                        reason="weight",
                        retry_after_s=1,
                    )
                )
                continue
            if org.held + head.weight > c:
                break
            org.queue.popleft()
            org.waiting -= head.weight
            org.held += head.weight
            head.granted = True
            head.ready.set_result(None)

    async def enter(self, work: VectorWork, wait_s: float) -> tuple[_Org, _Waiter | None]:
        c, w = _limits()
        if work.weight > c:
            raise VectorAdmissionError(
                "vector statement exceeds the org work ceiling", reason="weight", retry_after_s=1
            )
        if self.population + work.weight > 2 * w:
            raise VectorOverloadedError(
                "this process is at its vector work ceiling", reason="population", retry_after_s=1
            )
        # No org entry, future or suspension exists before this reservation.
        self.population += work.weight
        org: _Org | None = None
        waiter: _Waiter | None = None
        try:
            org = self.orgs.setdefault(work.org, _Org())
            self._advance(org)
            if not org.queue and org.held + work.weight <= c:
                org.held += work.weight
                return org, None
            if _without_wait.get() or org.waiting + work.weight > w:
                raise VectorAdmissionError(
                    "org vector capacity is unavailable", reason="org_queue", retry_after_s=1
                )
            waiter = _Waiter(work.weight, asyncio.get_running_loop().create_future())
            org.queue.append(waiter)
            org.waiting += work.weight
            try:
                async with asyncio.timeout(wait_s):
                    await waiter.ready
            except TimeoutError:
                raise VectorAdmissionError(
                    "the org vector wait expired", reason="org_deadline", retry_after_s=1
                ) from None
            return org, waiter
        except BaseException:
            self.population -= work.weight
            if org is not None:
                if waiter is not None:
                    if waiter.granted:
                        org.held -= work.weight
                    elif waiter in org.queue:
                        org.queue.remove(waiter)
                        org.waiting -= work.weight
                self._advance(org)
                self._forget(work.org, org)
            raise

    def _forget(self, name: str, org: _Org) -> None:
        if self.orgs.get(name) is org and not org.held and not org.queue:
            self.orgs.pop(name, None)

    def leave(self, work: VectorWork, org: _Org) -> None:
        org.held -= work.weight
        self.population -= work.weight
        self._advance(org)
        self._forget(work.org, org)


_admission = _Admission()


@asynccontextmanager
async def admit(*, pool_ready: bool, wait_s: float) -> AsyncIterator[None]:
    work = current_work()
    if work is None:
        yield
        return
    if not pool_ready or int(settings.ch_pool_maxsize) <= 0 or int(settings.ch_pool_wait_ms) <= 0:
        raise VectorOverloadedError(
            "the bounded ClickHouse gate is unavailable", reason="pool", retry_after_s=1
        )
    org, _ = await _admission.enter(work, wait_s)
    try:
        yield
    finally:
        _admission.leave(work, org)


def native_settings(out: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    if current_work() is None:
        return out
    caps = {
        "max_execution_time": timeout,
        "max_memory_usage": float(settings.ch_caller_shaped_max_memory_bytes),
        "max_rows_to_read": float(settings.vector_scan_max_rows_to_read),
    }
    for key, cap in caps.items():
        if not math.isfinite(cap) or cap <= 0:
            raise VectorAdmissionError(
                "vector native limits are unavailable", reason="configuration"
            )
        try:
            previous = float(out.get(key, 0))
        except (ValueError, TypeError):
            previous = 0
        bound = min(previous, cap) if math.isfinite(previous) and previous > 0 else cap
        out[key] = int(bound) if bound.is_integer() else bound
    out["read_overflow_mode"] = "throw"
    out["timeout_overflow_mode"] = "throw"
    out["result_overflow_mode"] = "throw"
    out["sort_overflow_mode"] = "throw"
    out["group_by_overflow_mode"] = "throw"
    out["distinct_overflow_mode"] = "throw"
    out["set_overflow_mode"] = "throw"
    out["join_overflow_mode"] = "throw"
    return out


def raise_native(error: Exception) -> None:
    if current_work() is None or isinstance(error, VectorCapacityError):
        return
    code = error_code(error)
    if code is None:
        return
    reasons: dict[int, VectorCapacityReason] = {
        158: "rows",
        307: "rows",
        191: "rows",
        396: "rows",
        241: "memory",
        159: "deadline",
    }
    reason = reasons.get(code)
    if reason:
        raise VectorAdmissionError(
            "the complete vector read exceeded its native budget", reason=reason, retry_after_s=1
        ) from error
