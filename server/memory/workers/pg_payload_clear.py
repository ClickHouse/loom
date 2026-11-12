"""Postgres payload clearing: drops the payload (`row`) of a memory revision once every
ClickHouse backup a restore may use holds it.

A revision ClickHouse came to hold more than the horizon ago (`effective_horizon_minutes`)
and that `loom.memory_versions` shows it holds, same key, version, id and content hash,
keeps its metadata and loses its payload: ClickHouse's history serves it from then on
(docs/architecture/storage-authority.md, "Payload clearing"). A profile revision keeps
its payload. A pass reads batches of LOOM_PG_DELIVERY_BATCH_ROWS until one comes back
short or half its deadline has gone; the next, in any process, resumes where it stopped
(the cursor in `loom.worker_status`), and one starts over after reaching the end. Idle
under the ClickHouse authority, and until this process adopted the deployment's setting
overrides. Off unless LOOM_PG_PAYLOAD_CLEAR_ENABLED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime

from server.core import pg, settings_overrides
from server.core.ch_errors import is_connection_failure, is_passing_state
from server.memory.store import history, pg_memory
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# Minutes the horizon keeps beyond the backup interval: the replica a backup reads lagging
# the one that took the insert, and clock skew between Postgres and the backup host
# (docs/operations/backup-and-recovery.md).
# not a setting: the documented horizon arithmetic is built on it
BACKUP_MARGIN_MINUTES = 60

# The clock the pass's budget reads and the wait between ClickHouse retries (seams for
# the tests).
_clock = time.monotonic
_sleep = asyncio.sleep
# Seconds between retries of a ClickHouse read.
_RETRY_WAIT_S = 1.0  # not a setting: the pass's budget bounds the retries, not the wait
# The cursor a pass that reached the end leaves: the next starts over. Not empty, since
# the supervisor hands a new pass the latest non-empty cursor.
_START = "start"


@dataclass
class ClearResult:
    # Revisions whose payload was cleared.
    cleared: int = 0
    # Revisions past the horizon that loom.memory_versions does not hold: kept, and
    # found again by the next pass that reaches them.
    missing_in_clickhouse: int = 0


def _cursor_text(after: tuple[datetime, str, str, int] | None) -> str:
    if after is None:
        return _START
    delivered, org, namespace, seq = after
    text = json.dumps([delivered.isoformat(), org, namespace, seq], ensure_ascii=False)
    if len(text) > pass_context.MAX_CURSOR:
        # Too long for the stored cursor: resume at the start of this delivery time, so
        # only the revisions delivered at that instant are read again.
        text = json.dumps([delivered.isoformat(), "", "", -1])
    return text


def _cursor_of(text: str) -> tuple[datetime, str, str, int] | None:
    """Where a pass starts from the cursor the last one left; None: from the beginning
    (none left, the end of the index reached, or one this build cannot read)."""
    if text in ("", _START):
        return None
    try:
        delivered, org, namespace, seq = json.loads(text)
        return (datetime.fromisoformat(delivered), str(org), str(namespace), int(seq))
    except (TypeError, ValueError):
        return None


def effective_horizon_minutes() -> int:
    """The configured horizon, never under the backup interval plus the margin, nor under
    the insert bound plus the margin; 0 configured is twice the backup interval plus
    the margin."""
    interval = settings.ch_backup_interval_minutes
    configured = settings.pg_payload_restore_horizon_minutes or 2 * interval + BACKUP_MARGIN_MINUTES
    insert_bound = math.ceil(pg.insert_bound_s() / 60)
    return max(configured, interval + BACKUP_MARGIN_MINUTES, insert_bound + BACKUP_MARGIN_MINUTES)


async def _held(
    candidates: list[pg_memory.Clearable], until: float, *, first: bool
) -> list[pg_memory.Clearable] | None:
    """`history.versions_held`, retried until `until` (monotonic) while ClickHouse does
    not answer (`is_connection_failure`) or answers with a state that clears by itself
    (`is_passing_state`). Past it, the first batch of a pass raises; a later one is noted
    as the pass's failure and answers None, and the pass ends with the batches it
    cleared."""
    retrying = False
    while True:
        try:
            return await history.versions_held(candidates)
        # Broad: a later batch ClickHouse cannot read ends the pass with what it cleared;
        # what does not clear by itself re-raises.
        except Exception as e:
            if not (is_connection_failure(e) or is_passing_state(e)):
                raise
            if _clock() + _RETRY_WAIT_S >= until:
                if first:
                    raise
                pass_context.note_failure(e, "versions_held")
                log.info("pg_payload_clear: ClickHouse read left for the next pass: %s", e)
                return None
            if not retrying:
                log.info(
                    "pg_payload_clear: ClickHouse read retried within the pass's budget: %s", e
                )
                retrying = True
            await _sleep(_RETRY_WAIT_S)


async def run_pass() -> ClearResult:
    from server.workers.registry import pass_deadline_seconds

    result = ClearResult()
    # Before the overrides are adopted, the horizon and backup interval read here are the
    # ones the process started with, not an operator's override of them.
    if not pg.enabled() or not settings_overrides.adopted():
        pass_context.mark_idle()
        return result
    # Batches, and ClickHouse's retries, within half the deadline: the pass ends with its
    # result rather than in the supervisor's cancellation.
    until = _clock() + pass_deadline_seconds("pg_payload_clear") / 2
    with record_pass("pg_payload_clear") as rec:
        horizon_s = effective_horizon_minutes() * 60.0
        batch = settings.pg_delivery_batch_rows
        seen = 0
        after = _cursor_of(pass_context.resume_cursor())
        while True:
            pass_context.check_stop()
            candidates = await pg_memory.clearable_revisions(horizon_s, after, batch)
            held = await _held(candidates, until, first=not seen)
            if held is None:
                break  # the cursor stays before this batch
            pass_context.check_stop()  # the read above may have outlasted the lease
            result.cleared += await pg_memory.clear_payloads(horizon_s, held)
            result.missing_in_clickhouse += len(candidates) - len(held)
            seen += len(candidates)
            if len(candidates) < batch:
                after = None
                break
            after = candidates[-1].cursor
            if _clock() >= until:
                break
        pass_context.set_cursor(_cursor_text(after))
        rec.outcome = {
            "cleared": result.cleared,
            "missing_in_clickhouse": result.missing_in_clickhouse,
        }
        if not seen:
            pass_context.mark_idle()
        elif result.cleared or result.missing_in_clickhouse:
            log.info(
                "pg_payload_clear: cleared %d revision payloads; %d past the horizon not in "
                "ClickHouse's history",
                result.cleared,
                result.missing_in_clickhouse,
            )
    return result
