"""Replays deferred `memory.set_from_messages` requests.

`memory.set_from_messages(defer=true)` stores the raw request in
`loom.memory_extraction_queue` and returns in tens of milliseconds. This
worker claims the oldest pending rows, rebuilds the exact input and runs the
unchanged inline handler — LLM extraction, embedding, conflict surfacing,
blacklist, evidence and chunk projections — then rewrites the row `done`.
A failure goes back to `pending` with `attempts+1`, claimed again after a
backoff (15 s, 1, 5, then 30 minutes), until `max_attempts`, then `failed`
with `last_error` for `memory.extraction_status` to show. A model provider or
gateway that cannot be reached (no connection, a timeout, 502/503/504) spends
no attempt: the row waits the same backoff. A held call (the org switched off,
the enqueuer's identity refused, asked to wait or unread) marks the row `held`
until the hold ends, no attempt spent.

On by default: `defer` is opt-in per call, so an enabled worker with an
empty queue costs one indexed SELECT per interval and no LLM call. Turning
the worker off while callers defer strands their saves as `pending`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from server.core.errors import caller_text
from server.llm.gateway import GatewayUnavailable
from server.memory.app import extraction_queue as q
from server.workers.holds import hold_seconds
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionQueueConfig:
    # Rows claimed per pass; each is one inline extraction (10–15 s).
    max_batches_per_pass: int = 8
    # A claim older than this is reclaimable (worker died mid-extraction).
    lease_seconds: int = 600
    # Attempts before a row is parked `failed`.
    max_attempts: int = 8
    # Test scoping under shared CH state; production runs unscoped.
    org_filter: str | None = None
    namespace_filter: str | None = None
    # Namespaces replayed at once. Within ONE namespace the claimed batches
    # run serially in session-date order, so a later session's extraction
    # sees the earlier sessions' facts in `existing_memories` (
    # on multi-session conversations, date-ordered ingest lifted multi-hop 57.1 → 62.4 against
    # four-way concurrent ingest of the same sessions). Across namespaces
    # nothing links, so they run in parallel for throughput.
    namespace_concurrency: int = 4


@dataclass
class ExtractionQueueResult:
    done: list[dict[str, Any]] = field(default_factory=list)
    written_memories: int = 0
    failures: list[str] = field(default_factory=list)
    # Claimed rows marked `held`, no attempt spent: their org's model calls were
    # switched off, or the enqueuer's identity was refused, asked to wait or unread.
    skipped: int = 0
    # Claimed rows put back with no attempt spent: the model provider or gateway
    # could not be reached.
    unreachable: int = 0
    # Requests dropped on their last day before the queue's TTL, still waiting.
    expired: int = 0
    # Requests of a switched-off org dropped after LOOM_MODEL_CALLS_OFF_MAX_WAIT_DAYS.
    dropped: int = 0

    @property
    def done_count(self) -> int:
        return len(self.done)


def _observation_key(row: dict[str, Any]) -> str:
    """The session date the request carried (ISO YYYY-MM-DD), '' when none.
    Read from the stored payload so the queue table needs no new column."""
    try:
        return str(json.loads(row.get("payload") or "{}").get("observation_date") or "")
    except (ValueError, TypeError, AttributeError):
        return ""


def order_within_namespace(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group claimed rows by (org, namespace); order each group by the
    session date the caller supplied, then by enqueue time. Rows without a
    date sort first (their enqueue order is the only signal), so an undated
    batch never waits behind a dated one it may precede."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((str(r.get("org") or ""), str(r.get("namespace") or "")), []).append(r)
    for key, group in groups.items():
        group.sort(key=lambda r: (_observation_key(r), r.get("enqueued_at") or 0))
        groups[key] = group
    return groups


# What a model provider or gateway that does not answer raises: no connection, a
# timeout, an answer cut off, no gateway token to be had.
_UNREACHABLE = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    GatewayUnavailable,
    TimeoutError,
)


def unreachable(e: BaseException) -> bool:
    """Whether a replay's failure is the model provider or gateway not answering
    (`_UNREACHABLE`, or a 502, 503 or 504): the row is not at fault, so it spends
    no attempt. A request it cannot make at all (a bad scheme) is not this."""
    if isinstance(e, _UNREACHABLE):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (502, 503, 504)


def _config_from_settings() -> ExtractionQueueConfig:
    from server.settings import settings

    return ExtractionQueueConfig(
        max_batches_per_pass=settings.extraction_queue_max_batches_per_pass,
        lease_seconds=settings.extraction_queue_lease_seconds,
        max_attempts=settings.extraction_queue_max_attempts,
        namespace_concurrency=settings.extraction_queue_namespace_concurrency,
    )


async def run_pass(cfg: ExtractionQueueConfig | None = None) -> ExtractionQueueResult:
    """One pass; the supervisor calls it with no config, which reads the
    `extraction_queue_*` settings at pass time."""
    from server.core.ch import get_client
    from server.llm import customer

    cfg = cfg or _config_from_settings()
    result = ExtractionQueueResult()
    ch = get_client()
    # What holds a replay with no attempt spent: switched off after the claim, the
    # switch unread, or the enqueuer's identity refused, asked to wait or unread. Not
    # `HOLDS`: a request naming no customer never will, so it spends its attempts.
    held: tuple[type[Exception], ...] = (customer.SwitchUnread, *customer.GROUP_HOLDS)

    async def _replay_one(row: dict[str, Any]) -> None:
        try:
            written = await q.replay_row(row)
        except held as e:
            # Held until the org is back on, the switch or identity is read, or the
            # gateway's hold is over; the other rows, and other identities', go on.
            await q.mark_held(row, reason=caller_text(e), seconds=hold_seconds(e), ch=ch)
            result.skipped += 1
            return
        except Exception as e:
            if unreachable(e):
                log.warning(
                    "extraction_queue: the model provider could not be reached for %s/%s "
                    "queue_id=%s (%s: %s); no attempt spent",
                    row.get("org"),
                    row.get("namespace"),
                    row.get("queue_id"),
                    type(e).__name__,
                    e,
                )
                await q.mark_unreachable(row, error=caller_text(e), ch=ch)
                result.unreachable += 1
                return
            log.exception(
                "extraction_queue replay failed for %s/%s queue_id=%s",
                row.get("org"),
                row.get("namespace"),
                row.get("queue_id"),
            )
            # last_error reaches memory.extraction_status readers: the error's kind,
            # or a Loom error's code and message. The text is in the log above.
            status = await q.mark_error(
                row, error=caller_text(e), max_attempts=cfg.max_attempts, ch=ch
            )
            result.failures.append(f"{row.get('queue_id')}: {status}")
            return
        await q.mark_done(row, written=written, ch=ch)
        result.done.append(
            {
                "org": row.get("org"),
                "namespace": row.get("namespace"),
                "queue_id": row.get("queue_id"),
                "messages": int(row.get("message_count") or 0),
                "written": written,
            }
        )
        result.written_memories += written

    sem = asyncio.Semaphore(max(1, int(cfg.namespace_concurrency)))

    async def _replay_group(group: list[dict[str, Any]]) -> None:
        # Serial inside the namespace: the linking dependency runs this way.
        async with sem:
            for row in group:
                await _replay_one(row)

    with record_pass("extraction_queue") as rec:
        # A switched-off org's requests stay pending until it is switched back
        # on, or until they have waited LOOM_MODEL_CALLS_OFF_MAX_WAIT_DAYS
        # since the org was switched off.
        off = await customer.switched_off_orgs()
        from server.settings import settings

        result.dropped = await q.drop_waiting(
            off,
            older_than_days=settings.model_calls_off_max_wait_days,
            ch=ch,
            org_filter=cfg.org_filter,
            namespace_filter=cfg.namespace_filter,
        )
        rows = await q.claim_batch(
            limit=cfg.max_batches_per_pass,
            lease_seconds=cfg.lease_seconds,
            ch=ch,
            org_filter=cfg.org_filter,
            namespace_filter=cfg.namespace_filter,
            exclude_orgs=off,
        )
        groups = order_within_namespace(rows)
        # A group can still raise (its mark_done / mark_error insert failing).
        # Wait for every group before failing the pass: a bare gather raises on
        # the first error and leaves the other namespaces replaying outside
        # the pass, into the next one, with their errors never retrieved.
        outcomes = await asyncio.gather(
            *(_replay_group(g) for g in groups.values()), return_exceptions=True
        )
        errors = [o for o in outcomes if isinstance(o, BaseException)]
        for (org, ns), o in zip(groups, outcomes, strict=True):
            if isinstance(o, Exception):
                log.error("extraction_queue group %s/%s failed", org, ns, exc_info=o)
        if errors:
            raise errors[0]
        # Whatever still waits on its last day before the table's TTL is dropped with
        # a record, never removed unseen.
        result.expired = await q.drop_expiring(
            ch=ch, org_filter=cfg.org_filter, namespace_filter=cfg.namespace_filter
        )
        rec.outcome = {
            "claimed": len(rows),
            "done": result.done_count,
            "written_memories": result.written_memories,
            "failed": len(result.failures),
            "skipped": result.skipped,
            "unreachable": result.unreachable,
            "dropped": result.dropped,
            "expired": result.expired,
        }

    if result.done_count or result.failures:
        log.info(
            "extraction_queue: %d batches done, %d memories, %d failed",
            result.done_count,
            result.written_memories,
            len(result.failures),
        )
    return result


__all__ = [
    "ExtractionQueueConfig",
    "ExtractionQueueResult",
    "order_within_namespace",
    "run_pass",
]
