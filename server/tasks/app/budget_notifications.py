"""Budget warnings use an explicitly configured namespace-owned task inbox."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from server.auth import machines
from server.core import lease_writes
from server.core.background import TaskSet
from server.core.ch import get_client
from server.erasure.app import ledger_reads
from server.erasure.domain.scopes import may_cover
from server.observability.budget_notifications import (
    BudgetWarning,
    writer_lease_name,
)
from server.tasks.app import wakeups
from server.tasks.store import budget_notifications as store
from server.tasks.store import repo

log = logging.getLogger(__name__)
_pending: dict[str, str] = {}
_serial: dict[str, asyncio.Lock] = {}
_tasks = TaskSet()


def event_id(warning: BudgetWarning) -> str:
    day = datetime.fromtimestamp(warning.observed_at, UTC).date().isoformat()
    key = json.dumps(
        [
            warning.org,
            warning.namespace,
            warning.inbox,
            warning.user_id,
            warning.cap,
            warning.share,
            day,
        ]
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"loom:budget.warning:{key}"))


def schedule(warning: BudgetWarning) -> None:
    key = event_id(warning)
    # This only bounds local work; the durable event and distributed claim deduplicate.
    if key in _pending or len(_pending) >= 32:
        return
    _pending[key] = warning.org
    serial = _serial.setdefault(warning.org, asyncio.Lock())

    def cleanup() -> None:
        _pending.pop(key, None)
        if warning.org not in _pending.values():
            _serial.pop(warning.org, None)

    async def run() -> None:
        try:
            from server.core.ch import set_query_tag

            set_query_tag("budget.warning")
            async with serial:
                await deliver(warning)
        except Exception as e:  # noqa: BLE001 — alerts must not fail a budget check
            log.warning("budget warning delivery failed: %s", e)

    coroutine = run()
    try:
        task = _tasks.spawn(coroutine, name="budget.warning")
        # A task cancelled before its first turn never enters the coroutine's finally.
        task.add_done_callback(lambda _: cleanup())
    except RuntimeError:
        coroutine.close()
        cleanup()
        raise


async def invalidated(warning: BudgetWarning) -> bool:
    # Aggregate spend includes other users too. Conservatively fence every user erasure.
    try:
        rows = await ledger_reads.unfinished_reaching(
            warning.org,
            warning.namespace,
            None,
            int(warning.observed_at),
            ledger_reads.LEDGER_CLOCK_MARGIN_S,
            skip_abandoned=False,
        )
    except Exception as e:  # noqa: BLE001 — an unreadable erasure ledger forbids delivery
        log.warning("budget warning erasure check failed: %s", e)
        return True
    return any(
        row["scope_type"] == "user_id"
        or may_cover(str(row["scope_type"]), str(row["scope_value"]), warning.namespace)
        for row in rows
    )


async def inbox_owned_by(org: str, namespace: str, inbox: str) -> bool:
    row = await repo.fetch_inbox_row(org, inbox)
    return bool(row and row["owner_namespace"] == namespace)


async def deliver(warning: BudgetWarning) -> bool:
    key = event_id(warning)
    if not await machines.fleet_at(get_client(), 107):
        return False
    async with AsyncExitStack() as stack:
        try:
            mark_uncertain = await stack.enter_async_context(
                lease_writes.fenced(writer_lease_name(warning.org))
            )
        except lease_writes.FenceUnavailableError:
            return False
        return await _deliver_claimed(warning, key, mark_uncertain)


async def _deliver_claimed(
    warning: BudgetWarning, key: str, mark_uncertain: Callable[[bool], None]
) -> bool:
    if not await inbox_owned_by(warning.org, warning.namespace, warning.inbox):
        return False
    if await invalidated(warning) or await store.event_seqs(warning.org, warning.inbox, key):
        return False
    seq = await repo.server_version()
    payload = {
        "namespace": warning.namespace,
        "user_id": warning.user_id or None,
        "cap_usd": warning.cap,
        "spent_24h_usd": None if warning.user_id else warning.spent,
        "share": warning.share,
    }
    row = {
        "org": warning.org,
        "namespace": warning.namespace,
        "inbox": warning.inbox,
        "task_id": key,
        "seq": seq,
        "event": store.EVENT,
        "from_status": "",
        "to_status": "",
        "actor": "process:budget_warning",
        "agent": "-",
        "user_id": warning.user_id or "-",
        "session_id": "-",
        "source_class": "derived",
        "from_subject": "",
        "fence": 0,
        "version": seq,
        "reason": "",
        "patch": json.dumps(payload, sort_keys=True),
        "note": "User budget warning" if warning.user_id else "Namespace budget warning",
        "ts": datetime.fromtimestamp(seq / 1_000_000, UTC).replace(tzinfo=None),
    }
    stale = False
    try:
        mark_uncertain(True)
        emitted = await store.insert_event(row)
        mark_uncertain(False)
    finally:
        stale = await invalidated(warning)
        if stale:
            await store.remove_event(row)
    if stale:
        return False
    if emitted:
        wakeups.notify(wakeups.inbox_key(warning.org, warning.inbox))
    return emitted
