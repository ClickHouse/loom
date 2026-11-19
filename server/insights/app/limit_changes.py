"""Stored-first changes to namespace rates and operation latency budgets."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from server.core import runtime_overrides as ro
from server.core.numbers import parse_budget_value
from server.core.ratelimit import Limit, get_limiter
from server.observability.budgets import get_overrides, stored_ms

log = logging.getLogger(__name__)


async def change_namespace_rate(
    namespace: str,
    op_class: str,
    *,
    capacity: float | None,
    rate: float | None,
    clear: bool,
    deadline: float | None = None,
    ch: Any = None,
) -> dict[str, Any]:
    limiter = get_limiter()
    bounded: dict[str, Any] = {} if deadline is None else {"deadline": deadline, "ch": ch}
    if deadline is not None:
        limiter.check_bounded_namespace_change(namespace, op_class, clear=clear)
    result: dict[str, Any] = {
        "ok": True,
        "scope": "namespace",
        "key": namespace,
        "op_class": op_class,
    }
    if clear:
        await ro.clear(ro.RATELIMIT_NAMESPACE, (namespace, op_class), **bounded)
        if deadline is not None:
            limiter.check_bounded_namespace_change(namespace, op_class, clear=True)
        limiter.clear_namespace_limit(namespace, op_class)
        return {**result, "cleared": True}
    if capacity is None or rate is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "capacity and rate required when clear=false",
            },
        )
    await ro.put(
        ro.RATELIMIT_NAMESPACE,
        (namespace, op_class),
        {"capacity": capacity, "rate": rate},
        **bounded,
    )
    if deadline is not None:
        limiter.check_bounded_namespace_change(namespace, op_class, clear=False)
    limiter.set_namespace_limit(namespace, op_class, Limit(capacity=capacity, rate=rate))
    return {**result, "capacity": capacity, "rate": rate}


async def change_budget(
    op: str,
    *,
    consumer: str | None,
    budget_ms: int | None,
    budget: int | str | None,
    clear: bool,
    deadline: float | None = None,
    ch: Any = None,
) -> dict[str, Any]:
    overrides = get_overrides()
    bounded: dict[str, Any] = {} if deadline is None else {"deadline": deadline, "ch": ch}
    adoption: dict[str, Any] = {} if deadline is None else {"bounded": True}
    if deadline is not None:
        overrides.check_bounded_change(op, consumer=consumer, clear=clear)
    if consumer:
        if clear:
            await ro.clear(ro.BUDGET_CONSUMER, (consumer, op or "*"), **bounded)
            overrides.clear_for_consumer(consumer, op, **adoption)
            return {"ok": True, "cleared": True, "op": op, "consumer": consumer}
        value = budget if budget is not None else budget_ms
        if value is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_FAILED",
                    "message": "budget_ms (ms) or budget ('<n>x') required when clear=false",
                },
            )
        if parse_budget_value(value) is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_FAILED",
                    "message": "budget must be a non-negative integer (ms) or '<n>x'",
                },
            )
        await ro.put(ro.BUDGET_CONSUMER, (consumer, op or "*"), value, **bounded)
        overrides.set_for_consumer(consumer, op, value, **adoption)
        return {
            "ok": True,
            "op": op,
            "consumer": consumer,
            "budget": value,
            "budget_ms": value if isinstance(value, int) else None,
        }
    if clear:
        await ro.clear(ro.BUDGET_OP, (op,), **bounded)
        overrides.clear(op, **adoption)
        return {"ok": True, "cleared": True, "op": op}
    if budget_ms is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "budget_ms required when clear=false",
            },
        )
    try:
        stored_ms(budget_ms)
    except ValueError as error:
        log.info("budget change validation refused: %s", error)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "budget_ms must be a whole number of ms >= 0",
            },
        ) from error
    await ro.put(ro.BUDGET_OP, (op,), budget_ms, **bounded)
    overrides.set(op, budget_ms, **adoption)
    return {"ok": True, "op": op, "budget_ms": budget_ms}
