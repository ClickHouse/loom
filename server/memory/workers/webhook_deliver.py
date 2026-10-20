"""Memory webhooks: POST the outbox's due events to their webhooks.

The write paths leave one loom.memory_webhook_outbox row per (event, webhook)
(server/memory/app/webhooks.py), and nothing rewrites it. Each pass takes the
due events, leases them (an attempt row whose `next_attempt_at` is the lease's
end, so an overlapping copy of the pass passes over them), POSTs each body
signed as the task webhooks are (`server.core.webhooks`), and appends the
outcome to loom.memory_webhook_attempts: `delivered` on a 2xx; otherwise the
attempt is counted and the next one scheduled with backoff, until
`memory_webhook_max_attempts` marks it `dead`. An attempt row holds the event
id and counters only, so an erasure that deletes an event's outbox row while a
pass is in flight leaves nothing a later pass can send.

Before sending, a pass skips (marks dead) an event whose memory has no history
left in loom.memory_versions: an erasure removed it, possibly one run by an
older build that does not know the outbox. The `webhook-id` header is the
event id: two copies of a pass that overlap before a lease lands can send an
event twice, and the receiver drops the second.
"""

from __future__ import annotations

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

# Events POSTed per pass: each POST may wait the whole ack window and the pass
# runs under a 120 s deadline, so ten always fit. The rest go next pass.
MAX_POSTS_PER_PASS = 10
# How long a leased event is passed over by another copy of the pass: past the
# pass deadline, so a crashed pass's events come back.
LEASE_SECONDS = 180
# How long an event waits, its attempts uncounted, while its secret is not set.
SECRET_WAIT_SECONDS = 60
USER_AGENT = "loom-memory-webhook/1"


@dataclass(frozen=True)
class DeliverResult:
    delivered: int = 0
    failed: int = 0
    dead: int = 0

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
    hook: Mapping[str, Any],
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
    return await signed.post(
        transport,
        str(hook["url"]),
        headers,
        body,
        schemes=url_schemes(),
        private_hosts=settings.memory_webhook_private_host_set(),
    )


async def _attempt(
    transport: httpx.AsyncBaseTransport | None,
    row: Mapping[str, Any],
    hook: Mapping[str, Any] | None,
    *,
    erased: bool,
    env: Mapping[str, str],
    now: datetime,
) -> dict[str, Any]:
    """The attempt row for one event: sent, or why not."""
    event_id, attempts = str(row["event_id"]), int(row["attempts"])
    if hook is None or erased:
        why = "the webhook was removed" if hook is None else "the memory was erased"
        return _attempt_row(
            event_id, status="dead", attempts=attempts, next_attempt_at=now, now=now, last_error=why
        )
    ref = str(hook["secret_ref"])
    secret = signed.read_secret(str(hook["org"]), ref, env)
    if secret is None:  # waits for the operator, as the task inbox webhook does
        return _attempt_row(
            event_id,
            status="pending",
            attempts=attempts,
            next_attempt_at=now + timedelta(seconds=SECRET_WAIT_SECONDS),
            now=now,
            last_error=f"no secret in env {ref}",
        )
    try:
        delivery = await _post(transport, row, hook, secret)
    except Exception as e:  # noqa: BLE001 — one event's bad secret or URL must not stop the rest
        # Type only: the message of a secret-decoding error must not reach a log line.
        delivery = signed.Delivery(0, type(e).__name__)
    return _outcome(row, delivery, now=now)


def _key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[c]) for c in store.KEY_COLS)


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
    due = await store.due_events(
        now,
        since=now - timedelta(days=store.KEEP_DAYS),
        limit=MAX_POSTS_PER_PASS,
        org=org,
        namespace=namespace,
    )
    if not due:
        return DeliverResult()
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
    hooks = {
        (str(h["org"]), str(h["namespace"]), str(h["webhook_id"])): h
        for h in await store.live_webhooks()
    }
    # memory.erased names no key; every other event names the memory it is about.
    keyed = {_key(r) for r in due if r["memory_key"]}
    kept = await store.keys_with_history(sorted(keyed))
    counts = {"delivered": 0, "pending": 0, "dead": 0}
    for row in due:
        hook = hooks.get((str(row["org"]), str(row["namespace"]), str(row["webhook_id"])))
        erased = bool(row["memory_key"]) and _key(row) not in kept
        out = await _attempt(transport, row, hook, erased=erased, env=env, now=now)
        await store.insert_attempts([out])
        counts[out["status"]] += 1
    return DeliverResult(
        delivered=counts["delivered"], failed=counts["pending"], dead=counts["dead"]
    )


async def run_pass() -> DeliverResult:
    with record_pass("memory_webhook_deliver") as rec:
        result = await deliver_once()
        rec.outcome = {"delivered": result.delivered, "failed": result.failed, "dead": result.dead}
    if result.total == 0:
        pass_context.mark_idle()
    else:
        log.info(
            "memory_webhook_deliver: delivered=%d failed=%d dead=%d",
            result.delivered,
            result.failed,
            result.dead,
        )
    return result
