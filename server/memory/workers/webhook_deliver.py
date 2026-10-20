"""Memory webhooks: POST the outbox's due events to their webhooks.

The write paths leave one loom.memory_webhook_outbox row per (event, webhook)
(server/memory/app/webhooks.py). Each pass takes the due rows, leases them
(a new version with `lease_until`, so an overlapping copy of the pass passes
over them), POSTs each event's body signed as the task webhooks are
(`server.core.webhooks`), and writes the outcome: `delivered` on a 2xx;
otherwise the attempt is counted and the next try is scheduled with backoff,
until `memory_webhook_max_attempts` marks it `dead` (`webhook.deliveries`
lists it). The `webhook-id` header is the event id: two copies of a pass that
overlap before a lease lands can send an event twice, and the receiver drops
the second.
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
# How long a leased row is passed over by another copy of the pass: past the
# pass deadline, so a crashed pass's rows come back.
LEASE_SECONDS = 180
USER_AGENT = "loom-memory-webhook/1"
_EPOCH = datetime(1970, 1, 1)


@dataclass(frozen=True)
class DeliverResult:
    delivered: int = 0
    failed: int = 0
    dead: int = 0

    @property
    def total(self) -> int:
        return self.delivered + self.failed + self.dead


def _outcome(
    row: Mapping[str, Any],
    *,
    now: datetime,
    status: int,
    error: str,
    delivered: bool,
    give_up: bool = False,
) -> dict[str, Any]:
    """The row's next version after one attempt, its lease released; `give_up`
    marks it dead whatever attempts it has left."""
    attempts = int(row["attempts"]) + 1
    if delivered:
        state, next_try = "delivered", now
    elif give_up or attempts >= settings.memory_webhook_max_attempts:
        state, next_try = "dead", now
    else:
        state, next_try = "pending", now + timedelta(seconds=backoff_seconds(attempts))
    return {
        **row,
        "status": state,
        "attempts": attempts,
        "next_try_at": next_try,
        "lease_until": _EPOCH,
        "last_status": status,
        "last_error": error[:512],
        "version": next_version(),
    }


async def _attempt(
    transport: httpx.AsyncBaseTransport | None,
    row: Mapping[str, Any],
    hook: Mapping[str, Any] | None,
    env: Mapping[str, str],
) -> signed.Delivery:
    """POST one event; the delivery's outcome, or why it was not sent."""
    if hook is None:
        return signed.Delivery(0, "the webhook was removed")
    org, ref = str(hook["org"]), str(hook["secret_ref"])
    secret = signed.read_secret(org, ref, env)
    if secret is None:
        return signed.Delivery(0, f"no secret in env {ref}")
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


async def deliver_once(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> DeliverResult:
    """One pass over the due events. `transport` replaces the network (tests);
    `now` is naive UTC."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    env = os.environ if env is None else env
    due = await store.due_outbox(now, limit=MAX_POSTS_PER_PASS)
    if not due:
        return DeliverResult()
    hooks = {
        (str(h["org"]), str(h["namespace"]), str(h["webhook_id"])): h
        for h in await store.live_webhooks()
    }
    lease = now + timedelta(seconds=LEASE_SECONDS)
    leased = [{**r, "lease_until": lease, "version": next_version()} for r in due]
    await store.insert_outbox(leased)
    # By the status each row ends the pass in; `pending` is a failure to retry.
    counts = {"delivered": 0, "pending": 0, "dead": 0}
    for row in leased:
        hook = hooks.get((str(row["org"]), str(row["namespace"]), str(row["webhook_id"])))
        try:
            delivery = await _attempt(transport, row, hook, env)
        except Exception as e:  # noqa: BLE001 — one event's bad secret or URL must not stop the rest
            # Type only: the message of a secret-decoding error must not reach a log line.
            delivery = signed.Delivery(0, type(e).__name__)
        out = _outcome(
            row,
            now=now,
            status=delivery.status,
            error=delivery.error,
            delivered=delivery.ok,
            give_up=hook is None,  # removed: nothing will ever take it
        )
        await store.insert_outbox([out])
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
