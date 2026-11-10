"""Erasure Saga recovery worker.

The erasure Saga (in `erasure/app/saga.py`) can strand a row in
`in_progress` (Control Plane restart mid-Saga) or `failed` (a step fails
closed on a transient backend error). This worker replays such rows from
their last `steps_completed` checkpoint; every step is idempotent, so
replaying past a green step is harmless. After `max_attempts` the row is
marked `failed` permanently and an audit row is emitted for ops review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from server.core.error_summary import error_summary
from server.core.errors import caller_text
from server.erasure.domain.failure_reason import (
    ABANDONED_REASON_PREFIX,
    LAST_REASON_SEP,
    caller_reason,
)
from server.erasure.store.rows import LedgerRow, ResumeRow, VerifyColumns, census_kept
from server.observability import audit
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryConfig:
    # No checkpoint for this long ⇒ stuck. Well past worst-case step duration.
    stale_threshold_seconds: int = 300
    max_attempts: int = 5
    # Cap per pass so a backlog doesn't monopolize the CH connection.
    batch_size: int = 10


class RecoveryError(TypedDict, total=False):
    """One saga a pass could not resume or abandon; `attempt` is set on a resume."""

    erasure_id: str
    attempt: int
    error: str


@dataclass
class RecoveryResult:
    scanned: int = 0
    resumed: int = 0
    completed: int = 0
    failed_permanently: int = 0
    errors: list[RecoveryError] = field(default_factory=list)
    # Under Postgres: erasures copied into loom.erasure_ledger from ClickHouse's ledger,
    # and ledger rows past LOOM_ERASURE_LEDGER_DAYS swept.
    ledger_copied: int = 0
    ledger_swept: int = 0


async def run_pass(cfg: RecoveryConfig | None = None) -> RecoveryResult:
    """One scan + recovery pass. Idempotent if no stuck Sagas exist."""
    cfg = cfg or RecoveryConfig()
    result = RecoveryResult()

    from server.erasure.store import ledger as erasure_ledger

    with record_pass("erasure_recovery") as rec:
        stuck = await erasure_ledger.list_stuck_in_progress(
            stale_threshold_seconds=cfg.stale_threshold_seconds,
            max_attempts=cfg.max_attempts,
            batch_size=cfg.batch_size,
        )
        result.scanned = len(stuck)

        for row in stuck:
            try:
                await _recover_one(row=row, cfg=cfg, result=result)
            except Exception as e:  # noqa: BLE001 — one saga's terminal write never stops the rest
                _note_abandon_failure(row, e, result)

        # Fail closed: a saga whose abandonment write failed on its last
        # attempt is past the budget `list_stuck_in_progress` reads within;
        # its terminal row and audit are retried here until they land.
        exhausted = await erasure_ledger.list_unabandoned_exhausted(
            stale_threshold_seconds=cfg.stale_threshold_seconds,
            max_attempts=cfg.max_attempts,
            batch_size=cfg.batch_size,
        )
        result.scanned += len(exhausted)
        for row in exhausted:
            try:
                await _abandon(
                    row=row,
                    cfg=cfg,
                    result=result,
                    attempts=int(row["attempts"]),
                    last_error=caller_reason(str(row.get("failure_reason") or "")),
                    completed=set(row.get("steps_completed") or []),
                    verify=_ledger_verify(row),
                )
            except Exception as e:  # noqa: BLE001 — the next pass retries this abandonment
                _note_abandon_failure(row, e, result)

        await _keep_pg_ledger(result, cfg)
        rec.outcome = {
            "scanned": result.scanned,
            "resumed": result.resumed,
            "completed": result.completed,
            "failed_permanently": result.failed_permanently,
            "ledger_copied": result.ledger_copied,
            "ledger_swept": result.ledger_swept,
        }

    if result.scanned:
        log.warning(
            "erasure_recovery: scanned=%d resumed=%d completed=%d failed=%d",
            result.scanned,
            result.resumed,
            result.completed,
            result.failed_permanently,
        )
    return result


async def _keep_pg_ledger(result: RecoveryResult, cfg: RecoveryConfig) -> None:
    """Under Postgres, `loom.erasure_ledger` kept whole: the erasures only ClickHouse
    records copied in (`pg_ledger.backfill`), then rows past their days swept."""
    from server.core import pg
    from server.erasure.store import pg_ledger

    if not pg.enabled():
        return
    # Apart: a copy that keeps failing (it reads ClickHouse) must not stop the sweep.
    try:
        result.ledger_copied = await pg_ledger.backfill(cfg.batch_size * 100)
    except Exception as e:  # noqa: BLE001 — the sagas' recovery above must not fail on it
        log.warning("erasure_recovery: the ledger copy failed: %s", error_summary(e))
        pass_context.note_failure(e, "erasure.ledger")
    try:
        result.ledger_swept = await pg_ledger.sweep(cfg.batch_size * 100)
    except Exception as e:  # noqa: BLE001 — the sagas' recovery above must not fail on it
        log.warning("erasure_recovery: the ledger sweep failed: %s", error_summary(e))
        pass_context.note_failure(e, "erasure.ledger")


async def _recover_one(
    *,
    row: ResumeRow,
    cfg: RecoveryConfig,
    result: RecoveryResult,
) -> None:
    """Resume one Saga. A saga error is caught here and counted; an error in
    the abandonment write (checkpoint, audit) propagates to the pass loop,
    which records it and moves on to the next saga."""
    from server.erasure.app.saga import (
        RECOVERY_WORKER_ACTOR,
        audit_namespace,
        input_from_ledger_row,
        request_row_template,
        run_saga,
    )

    erasure_id = str(row["id"])
    prev_attempts = int(row["attempts"])
    new_attempts = prev_attempts + 1
    previously_completed = set(row.get("steps_completed") or [])

    log.info(
        "erasure_recovery resuming id=%s attempt=%d completed=%s",
        erasure_id,
        new_attempts,
        sorted(previously_completed),
    )
    result.resumed += 1

    # What the terminal row keeps if this attempt spends the budget: the last
    # step-named error and the verify columns, not a bare `max_attempts (5)
    # exceeded` that hides the step's reason and reads `residue_count = 0`
    # over live rows.
    out: Any = None
    last_error = caller_reason(str(row.get("failure_reason") or ""))
    completed_now: set[str] = set(previously_completed)

    try:
        input_obj = input_from_ledger_row(row)
        request_row = request_row_template(
            erasure_id=erasure_id,
            input=input_obj,
            actor=row["actor"],
            requested_at=row["requested_at"],
            attempts=new_attempts,
        )

        out = await run_saga(
            input=input_obj,
            row=request_row,
            erasure_id=erasure_id,
            requested_at=row["requested_at"],
            previously_completed=previously_completed,
            # The last census: its tables' owner steps re-run
            # even though the checkpoint names them.
            residue=row.get("residue"),
        )
        if out.status == "completed":
            result.completed += 1
            await audit.emit(
                event_type="erasure.recovered",
                org=input_obj.org,
                namespace=audit_namespace(input_obj.scope_type, input_obj.scope_value),
                actor=RECOVERY_WORKER_ACTOR,
                subject={
                    "erasure_id": erasure_id,
                    "attempts": new_attempts,
                    "previously_completed": sorted(previously_completed),
                },
                request_id=erasure_id,
            )
        else:
            # out.status == "failed" already wrote its own checkpoint (step
            # reason + census tail); the row keeps getting picked up until
            # max_attempts.
            last_error = out.failure_reason or last_error
            completed_now |= set(out.steps_completed or [])
    except Exception as e:
        log.exception("erasure_recovery raised on id=%s", erasure_id)
        # The ledger's reason reaches the requester: `caller_text`'s kind, or a Loom
        # error's code and message. The text is in the server log (the line above).
        last_error = caller_text(e)
        result.errors.append(
            {"erasure_id": erasure_id, "attempt": new_attempts, "error": f"{type(e).__name__}: {e}"}
        )

    # Permanent-fail once attempts hit max and the Saga still didn't complete.
    if new_attempts >= cfg.max_attempts and (out is None or out.status != "completed"):
        from server.erasure.app.saga import verify_columns_from_output

        await _abandon(
            row=row,
            cfg=cfg,
            result=result,
            attempts=new_attempts,
            last_error=last_error,
            completed=completed_now,
            verify=verify_columns_from_output(out),
        )


def _ledger_verify(row: LedgerRow) -> VerifyColumns:
    """The last census on an exhausted saga's ledger row, for its terminal row; empty
    when no census ran, or it found nothing and left a store uncounted (`census_kept`)."""
    out = VerifyColumns()
    if not census_kept(row.get("verified_at"), row.get("residue_count")):
        return out
    if "residue_count" in row:
        out["residue_count"] = row["residue_count"]
    if "residue" in row:
        out["residue"] = row["residue"]
    if "census" in row:
        out["census"] = row["census"]
    out["verified_at"] = row.get("verified_at")
    return out


def _note_abandon_failure(row: ResumeRow, e: Exception, result: RecoveryResult) -> None:
    log.exception("erasure_recovery: terminal write failed on id=%s", row.get("id"))
    result.errors.append({"erasure_id": str(row.get("id")), "error": repr(e)[:512]})
    pass_context.note_failure(e, "erasure.abandon")


async def _abandon(
    *,
    row: ResumeRow,
    cfg: RecoveryConfig,
    result: RecoveryResult,
    attempts: int,
    last_error: str,
    completed: set[str],
    verify: VerifyColumns,
) -> None:
    """Record a saga as abandoned: the `erasure.abandoned` audit row, then the
    terminal `failed` checkpoint whose reason starts with
    ABANDONED_REASON_PREFIX. The checkpoint lands last because it is what
    stops `list_unabandoned_exhausted` from returning the saga: a failure
    between the two re-runs both next pass (an audit row may repeat; one is
    never missing)."""
    from server.erasure.app.saga import (
        RECOVERY_WORKER_ACTOR,
        audit_namespace,
        checkpoint,
        input_from_ledger_row,
        request_row_template,
    )

    erasure_id = str(row["id"])
    reason = f"{ABANDONED_REASON_PREFIX}{cfg.max_attempts}) exceeded"
    if last_error:
        reason = f"{reason}{LAST_REASON_SEP}{last_error}"
    await audit.emit(
        event_type="erasure.abandoned",
        org=row["org"],
        namespace=audit_namespace(str(row["scope_type"]), str(row["scope_value"])),
        actor=RECOVERY_WORKER_ACTOR,
        subject={
            "erasure_id": erasure_id,
            "attempts": attempts,
            "completed_steps": sorted(completed),
            "reason": "max_attempts exceeded",
            "last_error": last_error[:512],
            "residue_count": int(verify.get("residue_count", 0) or 0),
        },
        request_id=erasure_id,
    )
    abandoned_at = datetime.now(UTC).replace(tzinfo=None)
    await checkpoint(
        row=request_row_template(
            erasure_id=erasure_id,
            input=input_from_ledger_row(row),
            actor=row["actor"],
            requested_at=row["requested_at"],
            attempts=attempts,
        ),
        status="failed",
        steps_completed=sorted(completed),
        current_step=len(completed),
        failure_reason=reason,
        completed_at=abandoned_at,
        extra=verify,
    )
    result.failed_permanently += 1
    from server.core import pg

    if pg.enabled():
        from server.erasure.store import pg_ledger

        # After the checkpoint, as it is the record; a miss is copied by `backfill`.
        try:
            await pg_ledger.abandon(str(row["org"]), erasure_id, abandoned_at)
        except Exception as e:  # noqa: BLE001 — `pg_ledger.backfill` copies it next pass
            pass_context.note_failure(e, "erasure.ledger")


__all__ = ["RecoveryConfig", "RecoveryResult", "run_pass"]
