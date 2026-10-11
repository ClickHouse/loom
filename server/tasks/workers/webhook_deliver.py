"""Signed webhooks for the task inbox plane (wake-up layer).

For each inbox with a ``webhook_url`` the pass reads the ``task_events``
rows after the inbox's acknowledged cursor and POSTs one small notice per
batch — "inbox X has new events since seq N", with the event kinds and the
task ids, never the task text. The notice is a wake-up hint: the receiver
pulls with ``task.pull`` (the claim is the pull) and the tables stay the
source of truth, so a lost or duplicated notice costs one extra pull.

Signed per the Standard Webhooks spec: ``webhook-id`` (the batch's last
seq — the receiver's idempotency key), ``webhook-timestamp`` (unix
seconds) and ``webhook-signature: v1,<base64 HMAC-SHA256>`` over
``"{id}.{timestamp}.{body}"``. The secret is read from the environment
variable ``task_inboxes.webhook_secret_ref`` names (a ``whsec_`` prefix
marks a base64 key, as the spec's generators emit); it is never stored in
ClickHouse and never logged. Acknowledgement is any 2xx inside a 10 s
window; anything else retries with backoff (next pass, 5 s, 5 min, 30 min,
capped) tracked in ``loom.task_webhook_cursor``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx

from server.core.datetimes import to_naive_utc as _naive
from server.core.hosts import vet_host
from server.tasks.domain import cursors as task_cursors
from server.tasks.domain.states import WEBHOOK_SECRET_PREFIX
from server.tasks.store import repo as repo
from server.tasks.store.rows import TaskEventRow, WebhookCursorRow, WebhookInboxRow
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

ACK_TIMEOUT_SECONDS = 10.0
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


# ---------------------------------------------------------------- signing


def secret_bytes(raw: str) -> bytes:
    """The HMAC key: a ``whsec_``-prefixed value is base64 (the spec's
    generated form); anything else is the literal UTF-8 string."""
    if raw.startswith("whsec_"):
        return base64.b64decode(raw[len("whsec_") :])
    return raw.encode("utf-8")


def sign(secret: bytes, *, msg_id: str, timestamp: int, body: bytes) -> str:
    """``v1,<base64 HMAC-SHA256("{id}.{timestamp}.{body}")>``."""
    signed = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


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


def signed_headers(secret: bytes, *, msg_id: str, timestamp: int, body: bytes) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign(secret, msg_id=msg_id, timestamp=timestamp, body=body),
    }


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


async def pinned_target(url: str) -> tuple[str, str, str, str]:
    """(refusal, request URL, Host header, TLS server name) for a webhook URL. The
    host must resolve to public addresses only (`vet_host`, as for tracker
    sources), and the request goes to the address vetted here, so a second DNS
    answer cannot point it inward; the name stays the Host header, the SNI and the
    name the certificate is checked against. http and https only."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        return "webhook_url must be an http or https URL with a host", "", "", ""
    try:
        port = parts.port
    except ValueError:
        return "webhook_url has an invalid port", "", "", ""
    refusal, address = await vet_host(host)
    if refusal:
        return refusal, "", "", ""
    suffix = f":{port}" if port else ""
    literal = f"[{address}]" if ":" in address else address
    name = f"[{host}]" if ":" in host else host
    pinned = urlunsplit((parts.scheme, literal + suffix, parts.path, parts.query, ""))
    return "", pinned, name + suffix, host


async def _post(
    transport: httpx.AsyncBaseTransport | None, url: str, headers: dict[str, str], body: bytes
) -> int:
    """The HTTP status, or 0 when the URL is refused or no answer arrived inside
    the ack window. Redirects are not followed. Each delivery has its own client:
    a pooled connection is keyed by the pinned address, so a second inbox on the
    same address would reuse a TLS session verified for another name."""
    refusal, pinned, host_header, server_name = await pinned_target(url)
    if refusal:
        log.warning("task webhook: %s refused: %s", url, refusal)
        return 0
    try:
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            response = await client.post(
                pinned,
                content=body,
                headers={**headers, "Host": host_header},
                timeout=ACK_TIMEOUT_SECONDS,
                follow_redirects=False,
                extensions={"sni_hostname": server_name},
            )
    except httpx.HTTPError as e:
        log.info("task webhook: no answer from %s: %s", url, type(e).__name__)
        return 0
    return int(response.status_code)


def _secret_for(inbox_row: WebhookInboxRow, env: Mapping[str, str]) -> bytes | None:
    ref = str(inbox_row.get("webhook_secret_ref") or "")
    # A row written before the prefix rule may name any variable: never read it.
    raw = env.get(ref, "") if ref.startswith(WEBHOOK_SECRET_PREFIX) else ""
    if not raw:
        key = (str(inbox_row["org"]), str(inbox_row["inbox"]))
        if key not in _warned_missing_secret:
            _warned_missing_secret.add(key)
            log.warning(
                "task webhook: inbox %s/%s has a webhook_url but no secret in env %r "
                "(it must be set and start LOOM_WEBHOOK_SECRET_); notices are held until then",
                key[0],
                key[1],
                ref or "<unset>",
            )
        return None
    return secret_bytes(raw)


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
        watermark=_naive(now - timedelta(seconds=task_cursors.LAG_SECONDS)),
        inbox=inbox,
        limit=BATCH_LIMIT,
    )
    if not rows:
        return "skipped"
    body = notice_body(org, inbox, last_seq, rows)
    msg_id = str(int(rows[-1]["seq"]))
    headers = signed_headers(secret, msg_id=msg_id, timestamp=int(time.time()), body=body)
    status = await _post(transport, str(inbox_row["webhook_url"]), headers, body)
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
