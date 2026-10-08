"""Audit tamper-detection worker.

Recomputes `row_hash` for each recent `loom.audit_log` row and compares
against the stored hash (per-row tamper evidence, not a hash chain).
Mismatches are emitted as an `audit.tamper_detected` row, so a verifier
alarm itself lands in the audit trail being verified. Each break is
reported once: the row lists the keys of the breaks it reports
(`break_keys`), and a later pass leaves out a key an earlier row named.

Second leg: checkpoints over closed windows. Each pass
seals windows whose end is past the grace period — row count plus an
order-independent digest of their row hashes, chained to the previous
checkpoint — and recounts the most recent sealed windows. A recount that
comes up short is a deletion, which no per-row hash can show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.observability import audit
from server.observability.audit import row_matches_hash
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# Rows read per page of the window scan, and the most one pass reads. A
# window past the cap is reported (`truncated`) rather than read unbounded.
VERIFY_PAGE_ROWS = 10_000
VERIFY_MAX_ROWS = 500_000
# Break keys one tamper row lists; a break past it is reported next pass.
MAX_BREAK_KEYS = 1000


@dataclass(frozen=True)
class AuditVerifierConfig:
    look_back_seconds: int = 3600  # 1h: scan only recent partitions
    window_seconds: int = 3600
    grace_seconds: int = 600
    verify_windows: int = 24
    max_seal_per_pass: int = 48


@dataclass
class AuditVerifierResult:
    rows_checked: int = 0
    breaks: list[dict[str, Any]] = field(default_factory=list)
    windows_sealed: int = 0
    windows_verified: int = 0
    # The window held more than VERIFY_MAX_ROWS rows; the newest went unchecked.
    truncated: bool = False
    # Breaks this pass found that no earlier tamper row reported.
    new_breaks: int = 0
    # Checkpoint findings: {"window_start", "kind", ...}. 'deletion' and
    # 'modification' are tamper evidence; 'late_insert' is reported but is
    # not by itself tampering (a backfill or a very late async insert).
    checkpoint_breaks: list[dict[str, Any]] = field(default_factory=list)


def _config_from_settings() -> AuditVerifierConfig:
    from server.settings import settings as s

    return AuditVerifierConfig(
        window_seconds=int(s.audit_checkpoint_window_seconds),
        grace_seconds=int(s.audit_checkpoint_grace_seconds),
        verify_windows=int(s.audit_checkpoint_verify_windows),
        max_seal_per_pass=int(s.audit_checkpoint_max_seal_per_pass),
    )


async def run_checkpoints(
    cfg: AuditVerifierConfig, result: AuditVerifierResult, *, ch: Any = None, now: Any = None
) -> None:
    """Seal closed windows, then recount the recent sealed ones. Fail-open:
    a checkpoint error never blocks the per-row leg (logged, counted)."""
    from datetime import UTC, datetime

    from server.insights.domain import audit_checkpoints as svc
    from server.insights.store import audit as audit_repo

    now = (now or datetime.now(UTC)).replace(tzinfo=None)
    kw = {"ch": ch} if ch is not None else {}
    chain = await audit_repo.latest_checkpoints(max(cfg.verify_windows, 1), **kw)
    # --- verify what is already sealed -----------------------------------
    for brk in svc.chain_breaks(chain):
        result.checkpoint_breaks.append({**brk, "kind": "chain"})
    for cp in chain:
        n, digest = await audit_repo.window_stats(cp.window_start, cp.window_end, **kw)
        result.windows_verified += 1
        kind = svc.classify(cp, n, digest)
        if kind:
            result.checkpoint_breaks.append(
                {
                    "window_start": cp.window_start.isoformat(),
                    "kind": kind,
                    "sealed_count": cp.row_count,
                    "observed_count": n,
                }
            )
    # --- seal what has closed since ---------------------------------------
    last_end = chain[-1].window_end if chain else None
    prev_hash = chain[-1].checkpoint_hash if chain else ""
    first_ts = None if chain else await audit_repo.earliest_audit_ts(**kw)
    for ws, we in svc.windows_to_seal(
        last_end,
        first_ts,
        now,
        window_seconds=cfg.window_seconds,
        grace_seconds=cfg.grace_seconds,
        max_windows=cfg.max_seal_per_pass,
    ):
        n, digest = await audit_repo.window_stats(ws, we, **kw)
        cp = svc.seal(ws, we, n, digest, prev_hash)
        await audit_repo.insert_checkpoint(
            {
                "window_start": cp.window_start,
                "window_end": cp.window_end,
                "row_count": cp.row_count,
                "rows_digest": cp.rows_digest,
                "prev_checkpoint_hash": cp.prev_checkpoint_hash,
                "checkpoint_hash": cp.checkpoint_hash,
                "created_at": now,
            },
            **kw,
        )
        prev_hash = cp.checkpoint_hash
        result.windows_sealed += 1


def break_key(b: dict[str, Any]) -> str:
    """A break's identity: a row break by the row (its ts, request_id and
    the stored hash, which a second edit changes), a checkpoint break by its
    kind and window."""
    if "kind" in b:
        return f"{b['kind']}:{b.get('window_start', '')}"
    return f"row:{b['ts']}:{b['request_id']}:{b.get('row_hash', '')}"


async def _check_rows(cfg: AuditVerifierConfig, result: AuditVerifierResult) -> None:
    """Re-hash the look-back window page by page (keyset on ts, request_id,
    row_hash), holding one page in memory, at most VERIFY_MAX_ROWS rows."""
    from server.insights.store import audit as audit_repo

    after: tuple[Any, str, str] | None = None
    while True:
        page = await audit_repo.verify_window(
            cfg.look_back_seconds, after=after, limit=VERIFY_PAGE_ROWS
        )
        for r in page:
            result.rows_checked += 1
            if row_matches_hash(r):
                continue
            result.breaks.append(
                {
                    "partition": int(r["partition"]),
                    "ts": r["ts"].isoformat(),
                    "request_id": r["request_id"],
                    "row_hash": str(r.get("row_hash") or "").rstrip("\x00"),
                    "reason": "row_hash mismatch",
                }
            )
        if len(page) < VERIFY_PAGE_ROWS:
            return
        if result.rows_checked >= VERIFY_MAX_ROWS:
            result.truncated = True
            log.warning(
                "audit verifier: window holds more than %d rows; the rest is unchecked",
                VERIFY_MAX_ROWS,
            )
            return
        last = page[-1]
        after = (last["ts"], str(last["request_id"]), str(last["row_hash"]))


async def _report_new_breaks(cfg: AuditVerifierConfig, result: AuditVerifierResult) -> None:
    """One `audit.tamper_detected` row for the breaks no earlier row reported.
    A break stays inside the look-back (or the recounted windows) for many
    passes; re-alarming on it every pass would bury a new break among repeats."""
    from server.insights.store import audit as audit_repo

    tamper_kinds = ("deletion", "modification", "chain")
    tamper = [b for b in result.checkpoint_breaks if b.get("kind") in tamper_kinds]
    found = result.breaks + tamper
    if not found:
        return
    # Everything a pass can still find: the row window and the recounted windows.
    horizon = cfg.look_back_seconds + cfg.verify_windows * cfg.window_seconds + cfg.grace_seconds
    try:
        reported = await audit_repo.reported_tamper_keys(horizon)
    except Exception as e:  # noqa: BLE001 — unknown history reports every break (fail loud)
        log.warning("audit verifier: reported-break read failed; reporting all: %s", e)
        reported = set()
    new = [b for b in found if break_key(b) not in reported][:MAX_BREAK_KEYS]
    result.new_breaks = len(new)
    if not new:
        return
    new_rows = [b for b in new if "kind" not in b]
    new_windows = [b for b in new if "kind" in b]
    await audit.emit(
        event_type=audit_repo.TAMPER_EVENT,
        org="-",
        namespace="-",
        actor="audit-verifier-worker",
        subject={
            "rows_checked": result.rows_checked,
            "break_count": len(new_rows),
            "first_breaks": new_rows[:5],
            "checkpoint_break_count": len(new_windows),
            "first_checkpoint_breaks": new_windows[:5],
            "break_keys": [break_key(b) for b in new],
        },
    )


async def run_pass(cfg: AuditVerifierConfig | None = None) -> AuditVerifierResult:
    cfg = cfg or _config_from_settings()
    result = AuditVerifierResult()

    with record_pass("audit_verifier") as rec:
        await _check_rows(cfg, result)

        try:
            await run_checkpoints(cfg, result)
        except Exception as e:  # noqa: BLE001 — additive leg, never blocks the row check
            log.warning("audit verifier: checkpoint leg failed (fail-open): %s", e)
            pass_context.note_failure(e, "checkpoints")

        await _report_new_breaks(cfg, result)

        rec.outcome = {
            "rows_checked": result.rows_checked,
            "breaks": len(result.breaks),
            "windows_sealed": result.windows_sealed,
            "windows_verified": result.windows_verified,
            "checkpoint_breaks": len(result.checkpoint_breaks),
            "new_breaks": result.new_breaks,
            "truncated": result.truncated,
        }

    if result.breaks:
        log.warning(
            "audit verifier: %d break(s) in %d rows",
            len(result.breaks),
            result.rows_checked,
        )
    return result


__all__ = ["AuditVerifierConfig", "AuditVerifierResult", "run_checkpoints", "run_pass"]
