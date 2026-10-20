"""Memory webhooks: POST the outbox's due events to their webhooks.

The write paths leave one loom.memory_webhook_outbox row per (event, webhook)
(server/memory/app/webhooks.py), and nothing rewrites it. Each pass:

- marks dead, in one statement each, the events of removed webhooks and the
  events that waited `SECRET_DEADLINE` for a secret that was never set;
- takes the due events of the webhooks it can send to (registered, with a
  usable secret, not cooling down), at most `PER_WEBHOOK` of each, so one
  webhook's backlog never holds back another's events;
- leases them (an attempt row whose `next_attempt_at` is the lease's end, so an
  overlapping copy of the pass passes over them), POSTs `CONCURRENCY` at a
  time, each signed as the task webhooks are (`server.core.webhooks`), and
  appends each outcome to loom.memory_webhook_attempts: `delivered` on a 2xx,
  else `pending` with backoff until `memory_webhook_max_attempts` marks it
  `dead`.

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
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from server.core import webhooks as signed
from server.memory.app.webhooks import url_schemes
from server.memory.domain.webhook_events import backoff_seconds, event_body
from server.memory.store import webhooks as store
from server.memory.store.rows import next_version
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# POSTs per pass, CONCURRENCY at a time, each ended by the 10 s ack deadline:
# 40 take at most 100 s of the pass's 120 s deadline. The rest go next pass.
MAX_POSTS_PER_PASS = 40
CONCURRENCY = 4
# Events of one webhook per pass, so every webhook with work gets its turn.
PER_WEBHOOK = 10
# How long a leased event is passed over by another copy of the pass: past the
# pass deadline, so a crashed pass's events come back.
LEASE_SECONDS = 180
# An event that waited this long for its webhook's secret is marked dead before
# the outbox's 3-day TTL drops it, so webhook.deliveries says why.
SECRET_DEADLINE = timedelta(days=2, hours=12)
# After this many failed POSTs in a row a webhook is passed over for a cooldown
# that doubles from COOLDOWN_SECONDS up to MAX_COOLDOWN_SECONDS, reset by a 2xx.
FAILURES_BEFORE_COOLDOWN = 5
COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 3600
USER_AGENT = "loom-memory-webhook/1"


@dataclass
class _Health:
    failures: int = 0
    cooldown: int = 0
    until: datetime | None = None


# Per webhook, in this process only (best effort: a restart or another leader
# starts afresh, which costs a few more failed POSTs).
_health: dict[str, _Health] = {}


def _cooling(webhook_id: str, now: datetime) -> bool:
    h = _health.get(webhook_id)
    return h is not None and h.until is not None and now < h.until


def _note_post(webhook_id: str, ok: bool, now: datetime) -> None:
    if ok:
        _health.pop(webhook_id, None)
        return
    h = _health.setdefault(webhook_id, _Health())
    h.failures += 1
    if h.failures >= FAILURES_BEFORE_COOLDOWN:
        h.cooldown = min(h.cooldown * 2 or COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS)
        h.until = now + timedelta(seconds=h.cooldown)


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
    next_attempt_at: datetime,
    now: datetime,
    last_status: int = 0,
    last_error: str = "",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "status": status,
        "attempts": attempts,
        "next_attempt_at": next_attempt_at,
        "last_status": last_status,
        "last_error": last_error[:512],
        "recorded_at": now,
        "version": next_version(),
    }


def _outcome(row: Mapping[str, Any], delivery: signed.Delivery, *, now: datetime) -> dict[str, Any]:
    """The attempt row after one POST."""
    attempts = int(row["attempts"]) + 1
    if delivery.ok:
        status, next_at = "delivered", now
    elif attempts >= settings.memory_webhook_max_attempts:
        status, next_at = "dead", now
    else:
        status, next_at = "pending", now + timedelta(seconds=backoff_seconds(attempts))
    return _attempt_row(
        str(row["event_id"]),
        status=status,
        attempts=attempts,
        next_attempt_at=next_at,
        now=now,
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


async def deliver_once(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    org: str = "",
    namespace: str = "",
) -> DeliverResult:
    """One pass over the due events. `transport` replaces the network and
    `org` / `namespace` narrow the pass (tests); `now` is naive UTC."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    env = os.environ if env is None else env
    since = now - timedelta(days=store.KEEP_DAYS)

    def mine(hook: store.Hook) -> bool:
        return (not org or hook[0] == org) and (not namespace or hook[1] == namespace)

    live = [h for h in await store.live_webhooks() if mine(_hook(h))]
    secrets = {_hook(h): signed.read_secret(str(h["org"]), str(h["secret_ref"]), env) for h in live}
    urls = {_hook(h): str(h["url"]) for h in live}
    usable = {k: s for k, s in secrets.items() if s is not None}
    unsecured = [k for k in secrets if k not in usable]
    sendable = [k for k in usable if not _cooling(k[2], now)]

    removed = [h for h in await store.removed_webhooks() if mine(h)]
    await store.mark_dead(
        now, since=since, hooks=removed, reason="the webhook was removed", version=next_version()
    )
    await store.mark_dead(
        now,
        since=since,
        hooks=unsecured,
        reason="secret never set",
        before=now - SECRET_DEADLINE,
        version=next_version(),
    )
    due = await store.due_events(
        now, since=since, hooks=sendable, per_webhook=PER_WEBHOOK, limit=MAX_POSTS_PER_PASS
    )
    # The same event announced twice (a re-run) has one id: send it once.
    due = list({str(r["event_id"]): r for r in due}.values())
    waiting = len(live) - len(sendable)
    if not due:
        return DeliverResult(waiting=waiting)
    lease_end = now + timedelta(seconds=LEASE_SECONDS)
    await store.insert_attempts(
        [
            _attempt_row(
                str(r["event_id"]),
                status="leased",
                attempts=int(r["attempts"]),
                next_attempt_at=lease_end,
                now=now,
            )
            for r in due
        ]
    )
    # memory.erased names no key; every other event names the memory it is about.
    kept = await store.keys_with_history(sorted({_key(r) for r in due if r["memory_key"]}))
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(row: Mapping[str, Any]) -> str:
        if row["memory_key"] and _key(row) not in kept:
            out = _attempt_row(
                str(row["event_id"]),
                status="dead",
                attempts=int(row["attempts"]),
                next_attempt_at=now,
                now=now,
                last_error="the memory was erased",
            )
        else:
            hook = _hook(row)
            async with limit:
                delivery = await _post(transport, row, urls[hook], usable[hook])
            _note_post(hook[2], delivery.ok, now)
            out = _outcome(row, delivery, now=now)
        await store.insert_attempts([out])
        return str(out["status"])

    statuses = await asyncio.gather(*(one(r) for r in due))
    return DeliverResult(
        delivered=statuses.count("delivered"),
        failed=statuses.count("pending"),
        dead=statuses.count("dead"),
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
