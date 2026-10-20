"""Signed webhooks for the task inbox plane (wake-up layer).

For each inbox with a ``webhook_url`` the pass reads the ``task_events``
rows after the inbox's acknowledged cursor and POSTs one small notice per
batch — "inbox X has new events since seq N", with the event kinds and the
task ids, never the task text. The notice is a wake-up hint: the receiver
pulls with ``task.pull`` (the claim is the pull) and the tables stay the
source of truth, so a lost or duplicated notice costs one extra pull.

Signed per the Standard Webhooks spec (`server.core.webhooks`), with the
batch's last seq as ``webhook-id`` (the receiver's idempotency key). The
secret is read from the environment variable
``task_inboxes.webhook_secret_ref`` names, one of the inbox org's own.
Acknowledgement is any 2xx inside a 10 s window; anything else retries with
backoff (next pass, 5 s, 5 min, 30 min, capped) tracked in
``loom.task_webhook_cursor``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from server.core import webhooks
from server.core.datetimes import to_naive_utc as _naive
from server.tasks.domain import cursors as task_cursors
from server.tasks.store import repo as repo
from server.tasks.store.rows import TaskEventRow, WebhookCursorRow, WebhookInboxRow
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# Notices POSTed per pass. The pass runs under a 120 s deadline and each POST
# may wait the whole ack window, so ten is the most that always fit; inboxes
# past the cap go first next pass (they are ordered by their cursor's
# next_try_at, and a delivered inbox's moves to now).
MAX_POSTS_PER_PASS = 10
# The wait before the next try after the n-th consecutive failure: the next
# pass, then 5 s, 5 min, and 30 min for every failure after that.
BACKOFF_SECONDS: tuple[int, ...] = (0, 5, 300, 1800)
BATCH_LIMIT = 200
NOTICE_TYPE = "task.inbox.events"
USER_AGENT = "loom-task-webhook/1"

_warned_missing_secret: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class DeliverResult:
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    # Inboxes not reached because the pass hit MAX_POSTS_PER_PASS.
    deferred: int = 0

    @property
    def total(self) -> int:
        return self.delivered + self.failed + self.deferred


# ---------------------------------------------------------------- notice


def backoff_seconds(attempts: int) -> int:
    """Seconds until the next try after ``attempts`` consecutive failures."""
    if attempts <= 0:
        return 0
    return BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]


def notice_body(org: str, inbox: str, since_seq: int, rows: list[TaskEventRow]) -> bytes:
    """The notice: which inbox moved, the seq range, and per event the task
    id, kind and statuses — enough to decide whether to pull, no task text."""
    events = [
        {
            "seq": int(r["seq"]),
            "task_id": str(r["task_id"]),
            "event": str(r["event"]),
            "from_status": str(r.get("from_status") or ""),
            "to_status": str(r.get("to_status") or ""),
        }
        for r in rows
    ]
    body = {
        "type": NOTICE_TYPE,
        "org": org,
        "inbox": inbox,
        "since_seq": int(since_seq),
        "last_seq": events[-1]["seq"] if events else int(since_seq),
        "count": len(events),
        "events": events,
        "pull": "task.pull",
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------- the pass


def _cursor_row(
    org: str,
    inbox: str,
    *,
    last_seq: int,
    attempts: int,
    next_try_at: datetime,
    last_status: int,
) -> WebhookCursorRow:
    return {
        "org": org,
        "inbox": inbox,
        "last_seq": int(last_seq),
        "attempts": int(attempts),
        "next_try_at": _naive(next_try_at),
        "last_status": int(last_status),
        "version": repo.next_version(),
    }


def _secret_for(inbox_row: WebhookInboxRow, env: Mapping[str, str]) -> bytes | None:
    ref = str(inbox_row.get("webhook_secret_ref") or "")
    key = (str(inbox_row["org"]), str(inbox_row["inbox"]))
    # A row written before the org rule may name any variable: never read it.
    secret = webhooks.read_secret(key[0], ref, env)
    if secret is None:
        if key not in _warned_missing_secret:
            _warned_missing_secret.add(key)
            log.warning(
                "task webhook: inbox %s/%s has a webhook_url but no secret in env %r "
                "(it must be set and start %s); notices are held until then",
                key[0],
                key[1],
                ref or "<unset>",
                webhooks.webhook_secret_org_prefix(key[0]),
            )
    return secret


async def _deliver_inbox(
    transport: httpx.AsyncBaseTransport | None,
    inbox_row: WebhookInboxRow,
    cursor: WebhookCursorRow | None,
    *,
    now: datetime,
    env: Mapping[str, str],
) -> str:
    """One inbox: ``"delivered"``, ``"failed"`` or ``"skipped"`` (nothing new,
    a retry not yet due, or no secret)."""
    org, inbox = str(inbox_row["org"]), str(inbox_row["inbox"])
    last_seq = int(cursor["last_seq"]) if cursor else 0
    if cursor and _naive(cursor["next_try_at"]) > _naive(now):
        return "skipped"
    secret = _secret_for(inbox_row, env)
    if secret is None:
        return "skipped"
    rows = await repo.events_since(
        org,
        since_seq=last_seq,
        lag_s=task_cursors.LAG_SECONDS,
        inbox=inbox,
        limit=BATCH_LIMIT,
    )
    if not rows:
        return "skipped"
    body = notice_body(org, inbox, last_seq, rows)
    msg_id = str(int(rows[-1]["seq"]))
    headers = webhooks.signed_headers(
        secret, msg_id=msg_id, timestamp=int(time.time()), body=body, user_agent=USER_AGENT
    )
    status = (await webhooks.post(transport, str(inbox_row["webhook_url"]), headers, body)).status
    if 200 <= status < 300:
        await repo.insert_webhook_cursor(
            _cursor_row(
                org, inbox, last_seq=int(msg_id), attempts=0, next_try_at=now, last_status=status
            )
        )
        return "delivered"
    await _record_failure(org, inbox, cursor, now=now, status=status)
    return "failed"


async def _record_failure(
    org: str, inbox: str, cursor: WebhookCursorRow | None, *, now: datetime, status: int
) -> None:
    """Keep the cursor, count the attempt and schedule the retry."""
    last_seq = int(cursor["last_seq"]) if cursor else 0
    attempts = (int(cursor["attempts"]) if cursor else 0) + 1
    await repo.insert_webhook_cursor(
        _cursor_row(
            org,
            inbox,
            last_seq=last_seq,
            attempts=attempts,
            next_try_at=now + timedelta(seconds=backoff_seconds(attempts)),
            last_status=status,
        )
    )
    log.info(
        "task webhook: inbox %s/%s status=%d attempt=%d next in %ds",
        org,
        inbox,
        status,
        attempts,
        backoff_seconds(attempts),
    )


async def deliver_once(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> DeliverResult:
    """One pass over every inbox with a webhook_url. `transport` replaces the
    network (tests); each delivery gets a fresh client on it."""
    now = now or datetime.now(UTC)
    env = os.environ if env is None else env
    inboxes = await repo.list_webhook_inboxes()
    if not inboxes:
        return DeliverResult()
    cursors = {(str(c["org"]), str(c["inbox"])): c for c in await repo.fetch_webhook_cursors()}
    counts = {"delivered": 0, "failed": 0, "skipped": 0, "deferred": 0}

    def _due_at(row: WebhookInboxRow) -> datetime:
        c = cursors.get((str(row["org"]), str(row["inbox"])))
        return _naive(c["next_try_at"]) if c else datetime.min

    for i, row in enumerate(sorted(inboxes, key=_due_at)):
        if counts["delivered"] + counts["failed"] >= MAX_POSTS_PER_PASS:
            counts["deferred"] = len(inboxes) - i
            break
        org, inbox = str(row["org"]), str(row["inbox"])
        cursor = cursors.get((org, inbox))
        try:
            outcome = await _deliver_inbox(transport, row, cursor, now=now, env=env)
        except Exception as e:  # noqa: BLE001 — one inbox's bad URL or secret must not stop the rest
            # Type only: the message of a secret-decoding error must not reach a log line.
            log.warning("task webhook: inbox %s/%s failed: %s", org, inbox, type(e).__name__)
            outcome = "failed"
            try:
                await _record_failure(org, inbox, cursor, now=now, status=0)
            except Exception as rec_err:  # noqa: BLE001 — the backoff row is retried next pass
                log.warning(
                    "task webhook: inbox %s/%s failure row not written: %s",
                    org,
                    inbox,
                    type(rec_err).__name__,
                )
        counts[outcome] += 1
    return DeliverResult(**counts)


async def run_pass() -> DeliverResult:
    with record_pass("task_webhook_deliver") as rec:
        result = await deliver_once()
        rec.outcome = {
            "delivered": result.delivered,
            "failed": result.failed,
            "skipped": result.skipped,
            "deferred": result.deferred,
        }
    if result.total == 0:
        pass_context.mark_idle()
    else:
        log.info(
            "task_webhook_deliver: delivered=%d failed=%d skipped=%d deferred=%d",
            result.delivered,
            result.failed,
            result.skipped,
            result.deferred,
        )
    return result
