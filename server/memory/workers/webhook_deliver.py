"""Memory webhooks: POST the outbox's due events to their webhooks.

The write paths leave one loom.memory_webhook_outbox row per (event, webhook)
(server/memory/app/webhooks.py), and nothing rewrites it. Each pass:

- marks dead, in one statement each, the events of removed webhooks and the
  events that waited `SECRET_DEADLINE` for a usable secret, and those a
  receiver did not take within `DELIVERY_DEADLINE`;
- takes the due events of the webhooks it can send to (registered, with a
  usable secret, not cooling down) in turns: every webhook's oldest first, then
  every webhook's second, at most `PER_WEBHOOK` of each and
  `MAX_POSTS_PER_PASS` in all;
- leases them (an attempt row whose `next_attempt_at` is the lease's end, so an
  overlapping copy of the pass passes over them), then POSTs them, `CONCURRENCY`
  at a time and one at a time per webhook, so a slow receiver holds one slot.
  Before each POST it checks the webhook's cooldown and the key's history.
  Each body is signed as the task webhooks' are (`server.core.webhooks`);
- appends every outcome to loom.memory_webhook_attempts in one insert:
  `delivered` on a 2xx, else `pending` with backoff until
  `memory_webhook_max_attempts` marks it `dead`.

An attempt row holds the event id and counters only, so an erasure that
deletes an event's outbox row while a pass is in flight leaves nothing a later
pass can send. A pass skips (marks dead) an event whose key has no history left
in loom.memory_versions: an erasure removed it, possibly one run by an older
build that does not know the outbox. `webhook-id` is the event id: a repeat of
an event (two overlapping passes, a retry) carries the same id and the receiver
drops it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from server.core import webhooks as signed
from server.core.datetimes import naive_utc_now
from server.memory.app.webhooks import url_schemes
from server.memory.domain.webhook_events import backoff_seconds, event_body
from server.memory.store import webhooks as store
from server.memory.store.rows import next_version
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# POSTs per pass, CONCURRENCY at a time, each ended by the ack deadline: together they
# fill POST_BUDGET_SECONDS, inside MAX_PASS_SECONDS, and the pass records what it sent;
# the rest go next pass.
MAX_POSTS_PER_PASS = 40  # not a setting: with CONCURRENCY and the ack deadline, fills the budget
CONCURRENCY = 4  # not a setting: with MAX_POSTS_PER_PASS and the ack deadline, fills the budget
# not a setting: derived from the three values it is made of
POST_BUDGET_SECONDS = MAX_POSTS_PER_PASS / CONCURRENCY * signed.ACK_TIMEOUT_SECONDS
# The pass deadline: the POST budget plus room for the reads before and the insert after.
MAX_PASS_SECONDS = POST_BUDGET_SECONDS + 20  # not a setting: derived from POST_BUDGET_SECONDS
# Events of one webhook per pass, so every webhook with work gets its turn.
PER_WEBHOOK = 10  # not a setting: a webhook's share of MAX_POSTS_PER_PASS
# How long a leased event is passed over by another copy of the pass: past the
# pass deadline, so a crashed pass's events come back.
LEASE_SECONDS = MAX_PASS_SECONDS + 60  # not a setting: derived from MAX_PASS_SECONDS
# Events still waiting this long are marked dead before the outbox's 3-day TTL drops
# them, so webhook.deliveries says why: for a usable secret, and for a receiver to take
# them (a receiver that is down, or one whose backlog never reached its turn).
# memory_webhook_max_attempts lets an operator give up sooner.
SECRET_DEADLINE = timedelta(days=2, hours=12)  # not a setting: ends before store.KEEP_DAYS
DELIVERY_DEADLINE = timedelta(days=2, hours=18)  # not a setting: ends before store.KEEP_DAYS
USER_AGENT = "loom-memory-webhook/1"


@dataclass
class _Health:
    failures: int = 0
    cooldown: int = 0
    until: datetime | None = None


# Per webhook, in this process only (best effort: a restart or another leader
# starts afresh, which costs a few more failed POSTs).
_health: dict[str, _Health] = {}


def _cooling_until(webhook_id: str) -> datetime | None:
    h = _health.get(webhook_id)
    if h is None or h.until is None or naive_utc_now() >= h.until:
        return None
    return h.until


def _note_post(webhook_id: str, ok: bool) -> None:
    """A POST's outcome on the webhook's health."""
    if ok:
        _health.pop(webhook_id, None)
        return
    h = _health.setdefault(webhook_id, _Health())
    h.failures += 1
    if h.failures >= settings.memory_webhook_failures_before_cooldown:
        h.cooldown = min(
            h.cooldown * 2 or settings.memory_webhook_cooldown_seconds,
            settings.memory_webhook_max_cooldown_seconds,
        )
        h.until = naive_utc_now() + timedelta(seconds=h.cooldown)


@dataclass(frozen=True)
class DeliverResult:
    delivered: int = 0
    failed: int = 0
    dead: int = 0
    # Webhooks passed over: no usable secret yet, or cooling down.
    waiting: int = 0

    @property
    def total(self) -> int:
        return self.delivered + self.failed + self.dead


def _attempt_row(
    event_id: str,
    *,
    status: str,
    attempts: int,
    next_attempt_at: datetime | None = None,
    last_status: int = 0,
    last_error: str = "",
) -> dict[str, Any]:
    """An attempt row recorded now; next_attempt_at defaults to now (a settled event)."""
    now = naive_utc_now()
    return {
        "event_id": event_id,
        "status": status,
        "attempts": attempts,
        "next_attempt_at": next_attempt_at or now,
        "last_status": last_status,
        "last_error": last_error[:512],
        "recorded_at": now,
        "version": next_version(),
    }


def _outcome(row: Mapping[str, Any], delivery: signed.Delivery) -> dict[str, Any]:
    """The attempt row after one POST; a retry is due a backoff after the failure."""
    attempts = int(row["attempts"]) + 1
    next_at = None
    if delivery.ok:
        status = "delivered"
    elif attempts >= settings.memory_webhook_max_attempts:
        status = "dead"
    else:
        status = "pending"
        next_at = naive_utc_now() + timedelta(seconds=backoff_seconds(attempts))
    return _attempt_row(
        str(row["event_id"]),
        status=status,
        attempts=attempts,
        next_attempt_at=next_at,
        last_status=delivery.status,
        last_error=delivery.error,
    )


async def _post(
    transport: httpx.AsyncBaseTransport | None,
    row: Mapping[str, Any],
    url: str,
    secret: bytes,
) -> signed.Delivery:
    body = event_body(row)
    headers = signed.signed_headers(
        secret,
        msg_id=str(row["event_id"]),
        timestamp=int(time.time()),
        body=body,
        user_agent=USER_AGENT,
    )
    try:
        return await signed.post(
            transport,
            url,
            headers,
            body,
            schemes=url_schemes(),
            private_hosts=settings.memory_webhook_private_host_set(),
        )
    except Exception as e:  # noqa: BLE001 — one event's bad URL must not stop the rest
        return signed.Delivery(0, type(e).__name__)


def _key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[c]) for c in store.KEY_COLS)


def _hook(row: Mapping[str, Any]) -> store.Hook:
    return (str(row["org"]), str(row["namespace"]), str(row["webhook_id"]))


def _wait_row(row: Mapping[str, Any], *, until: datetime, why: str) -> dict[str, Any]:
    """An event passed over, its attempts uncounted, until `until`; the receiver's
    last status stays and `why` is the error."""
    return _attempt_row(
        str(row["event_id"]),
        status="pending",
        attempts=int(row["attempts"]),
        next_attempt_at=until,
        last_status=int(row["last_status"]),
        last_error=why,
    )


async def _skip_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The attempt row to record instead of a POST (a cooldown wait, an erased key),
    or None to send it."""
    until = _cooling_until(_hook(row)[2])
    if until is not None:  # failed its last POSTs earlier in this pass
        return _wait_row(row, until=until, why="the webhook is cooling down")
    # memory.erased names no key; any other event names the memory it is about.
    # Read right before the POST: only an erasure during the POST can let it out.
    if row["memory_key"] and not await store.keys_with_history([_key(row)]):
        return _attempt_row(
            str(row["event_id"]),
            status="dead",
            attempts=int(row["attempts"]),
            last_error="the memory was erased",
        )
    return None


async def _send(
    transport: httpx.AsyncBaseTransport | None,
    due: list[dict[str, Any]],
    urls: Mapping[store.Hook, str],
    secrets: Mapping[store.Hook, bytes],
) -> tuple[list[dict[str, Any]], int]:
    """POST the leased `due` events: CONCURRENCY at a time, one at a time per
    webhook, within POST_BUDGET_SECONDS. Returns the attempt rows of the events
    that finished (recorded even when the budget or an error cut the pass) and
    the number of POSTs that failed and will be retried."""
    slots = asyncio.Semaphore(CONCURRENCY)
    finished: list[dict[str, Any]] = []
    failed = 0

    async def one(row: Mapping[str, Any]) -> None:
        """One event: checked once it holds a slot, then POSTed or passed over."""
        nonlocal failed
        hook = _hook(row)
        async with slots:
            try:
                skipped = await _skip_row(row)
            except Exception as e:  # noqa: BLE001 — one event's check must not stop the pass
                # It keeps its lease and goes again once the lease ends.
                log.warning("memory_webhook_deliver: %s not checked: %s", row["event_id"], e)
                return
            if skipped is not None:
                finished.append(skipped)
                return
            delivery = await _post(transport, row, urls[hook], secrets[hook])
        _note_post(hook[2], delivery.ok)
        outcome = _outcome(row, delivery)
        finished.append(outcome)
        if outcome["status"] == "pending":
            failed += 1

    async def in_turn(rows: list[Mapping[str, Any]]) -> None:
        """A webhook's events one after another: one POST in flight per webhook."""
        for row in rows:
            await one(row)

    by_hook: dict[store.Hook, list[Mapping[str, Any]]] = {}
    for row in due:
        by_hook.setdefault(_hook(row), []).append(row)
    try:
        # A task group: an error in one webhook's turn cancels the others' before
        # the pass returns, so no POST outlives the pass.
        async with asyncio.timeout(POST_BUDGET_SECONDS), asyncio.TaskGroup() as group:
            for rows in by_hook.values():
                group.create_task(in_turn(rows))
    except TimeoutError:
        # The unfinished keep their lease and go again once it ends, under the same ids.
        log.info("memory_webhook_deliver: %d of %d finished in the budget", len(finished), len(due))
    finally:
        # One insert per pass, whatever ended it: receivers drop a re-send of an id.
        await store.insert_attempts(finished)
    return finished, failed


async def deliver_once(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
    org: str = "",
    namespace: str = "",
) -> DeliverResult:
    """One pass over the due events. `transport` replaces the network, `env`
    the environment the secrets are read from, and `org` / `namespace` narrow
    the pass (tests). Every time comes from
    `naive_utc_now()`, read when it is needed."""
    now = naive_utc_now()
    since = now - timedelta(days=store.KEEP_DAYS)

    def mine(hook: store.Hook) -> bool:
        return (not org or hook[0] == org) and (not namespace or hook[1] == namespace)

    live = [h for h in await store.live_webhooks() if mine(_hook(h))]
    secrets = {_hook(h): signed.read_secret(str(h["org"]), str(h["secret_ref"]), env) for h in live}
    urls = {_hook(h): str(h["url"]) for h in live}
    usable = {k: s for k, s in secrets.items() if s is not None}
    unsecured = [k for k in secrets if k not in usable]
    sendable = [k for k in usable if _cooling_until(k[2]) is None]

    removed = [h for h in await store.removed_webhooks(since) if mine(h)]
    await store.mark_dead(
        now, since=since, hooks=removed, reason="the webhook was removed", version=next_version()
    )
    await store.mark_dead(
        now,
        since=since,
        hooks=unsecured,
        reason="no usable secret",
        before=now - SECRET_DEADLINE,
        version=next_version(),
    )
    await store.mark_dead(
        now,
        since=since,
        hooks=list(usable),
        reason="not delivered in time",
        before=now - DELIVERY_DEADLINE,
        version=next_version(),
    )
    due = await store.due_events(
        now, since=since, hooks=sendable, per_webhook=PER_WEBHOOK, limit=MAX_POSTS_PER_PASS
    )
    waiting = len(live) - len(sendable)
    if not due:
        return DeliverResult(waiting=waiting)
    lease_end = naive_utc_now() + timedelta(seconds=LEASE_SECONDS)
    await store.insert_attempts(
        [
            # A lease keeps the last attempt's status and error for webhook.deliveries.
            _attempt_row(
                str(r["event_id"]),
                status="leased",
                attempts=int(r["attempts"]),
                next_attempt_at=lease_end,
                last_status=int(r["last_status"]),
                last_error=str(r["last_error"]),
            )
            for r in due
        ]
    )
    finished, failed = await _send(transport, due, urls, usable)
    return DeliverResult(
        delivered=sum(out["status"] == "delivered" for out in finished),
        failed=failed,
        dead=sum(out["status"] == "dead" for out in finished),
        waiting=waiting,
    )


async def run_pass() -> DeliverResult:
    with record_pass("memory_webhook_deliver") as rec:
        result = await deliver_once()
        rec.outcome = {
            "delivered": result.delivered,
            "failed": result.failed,
            "dead": result.dead,
            "waiting": result.waiting,
        }
    if result.total == 0:
        pass_context.mark_idle()
    else:
        log.info(
            "memory_webhook_deliver: delivered=%d failed=%d dead=%d waiting=%d",
            result.delivered,
            result.failed,
            result.dead,
            result.waiting,
        )
    return result
