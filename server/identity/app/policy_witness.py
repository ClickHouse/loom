"""History a policy rule counts is committed before the call that made it answers.

A rule's leaves read `loom.agent_events` (and `breadth_within`, `loom.memory_exposures`).
Those rows normally wait in this replica's buffer, so the caller's next call, landing
on another replica, would be decided without them. A request whose event a rule of its
namespace names (an operation a `formerly` / `count_within` / `not_since` leaf lists, or
any read a `breadth_within` leaf counts) instead commits its row, stamped with the
server's `now64()`, in the request tail (`server.core.request_tail`): before the answer
starts. It is an async insert the tail waits for, so the rows of concurrent requests
share a part. The gate reads the trace with sequential consistency.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from server.core import request_tail
from server.core.ch_writes import batched_insert_settings
from server.identity.domain.policy_rules import (
    All,
    AnyOf,
    BreadthWithin,
    CountWithin,
    Formerly,
    Not,
    NotSince,
    PolicyRule,
)

log = logging.getLogger("loom.policy_witness")

_SENTINEL = ("", "-", "*")


def _policies_on() -> bool:
    from server.settings import settings

    return str(settings.policy_mode) != "off"


def named_ops(cond: Any) -> tuple[set[str], bool]:
    """The operations a condition's event leaves read, and whether it has a
    breadth leaf (whose `operation` list narrows exposures, empty = every read)."""
    if isinstance(cond, NotSince):
        return set(cond.operation) | set(cond.anchor), False
    if isinstance(cond, (Formerly, CountWithin)):
        return set(cond.operation), False
    if isinstance(cond, BreadthWithin):
        return set(), True
    if isinstance(cond, (All, AnyOf)):
        ops: set[str] = set()
        breadth = False
        for c in cond.of:
            o, b = named_ops(c)
            ops |= o
            breadth = breadth or b
        return ops, breadth
    if isinstance(cond, Not):
        return named_ops(cond.of)
    return set(), False


def _breadth_ops(cond: Any) -> list[set[str]]:
    if isinstance(cond, BreadthWithin):
        return [set(cond.operation)]
    if isinstance(cond, (All, AnyOf)):
        return [s for c in cond.of for s in _breadth_ops(c)]
    if isinstance(cond, Not):
        return _breadth_ops(cond.of)
    return []


def event_named(rules: list[PolicyRule], operation: str) -> bool:
    return any(r.enabled and operation in named_ops(r.condition)[0] for r in rules)


def exposure_named(rules: list[PolicyRule], operation: str) -> bool:
    return any(
        r.enabled and (not ops or operation in ops)
        for r in rules
        for ops in _breadth_ops(r.condition)
    )


async def _rules(org: str, namespace: str) -> list[PolicyRule]:
    from server.identity.app.history_policy import rules_for

    return await rules_for(org, namespace)


async def _commit(table: str, row: dict[str, Any]) -> None:
    from server.core.ch import get_client

    row = {k: v for k, v in row.items() if k != "ts"}  # the column's DEFAULT: the server's clock
    await get_client().insert(table, [row], settings=batched_insert_settings())


def hold_event(event: Any) -> bool:
    """The ledger's hook: True when the event is taken (committed or queued in the
    request tail), False to queue it as usual."""
    if not _policies_on() or event.kind != "request":
        return False
    if event.org in _SENTINEL or event.namespace in _SENTINEL:
        return False

    async def _settle() -> None:
        from server.observability.events import get_emitter

        try:
            if event_named(await _rules(event.org, event.namespace), event.operation):
                await _commit("loom.agent_events", event.to_row())
                return
        except Exception as e:  # noqa: BLE001 — the row is queued instead: never lost
            log.warning("policy witness for %s not committed: %s", event.operation, e)
        get_emitter().emit(event)

    return request_tail.defer(_settle)


def hold_exposure(row: dict[str, Any], write: Callable[[], None]) -> bool:
    """`record_exposure`'s hook: True when the row is taken (committed in the request
    tail, or handed to `write`, which schedules its usual insert), False to write it
    as usual."""
    org, ns = str(row.get("org") or ""), str(row.get("namespace") or "")
    if not _policies_on() or org in _SENTINEL or ns in _SENTINEL:
        return False

    async def _settle() -> None:
        try:
            if exposure_named(await _rules(org, ns), str(row.get("operation") or "")):
                await _commit("loom.memory_exposures", row)
                return
        except Exception as e:  # noqa: BLE001 — the row is written the usual way instead
            log.warning("policy witness for an exposure not committed: %s", e)
        write()

    return request_tail.defer(_settle)


__all__ = ["event_named", "exposure_named", "hold_event", "hold_exposure", "named_ops"]
