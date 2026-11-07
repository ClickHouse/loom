"""What happens to the rows a run wrote when the run completes.

The notes a task produced are the task's, not the agent's. loom keeps the rows
— on interdependent multi-session tasks the next task needs what the last one
wrote — but a completed run's working facts should stop competing at full standing with
durable memory. Three retentions:

    keep    nothing happens (the default)
    demote  confidence scaled by the expiry demote factor now (the expiry
            `demote` post-action, applied immediately)
    expire  expires_at = now + N days, on_expire = 'demote': the expiry sweep
            demotes when the expiry passes. The sweep has no "recalled or
            verified since" exemption; the grace window is the lever.

Each row acted on is announced to the namespace's webhooks as `memory.revised`.
Never a tombstone. Rows that are pinned, verified, promoted to the shared
tier, locked, episodic or reflection are retained whatever the mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from server.core.datetimes import naive_utc_now
from server.memory.app import webhooks
from server.memory.domain.webhook_events import memory_change

log = logging.getLogger(__name__)

RETAIN_MODES: tuple[str, ...] = ("keep", "demote", "expire")

# Rows the run wrote but that are not the run's facts: an observation is the
# trace, a reflection is the run's own lesson (already scoped).
_NOT_FACTS = ("episodic", "reflection")


@dataclass
class Counts:
    retained: int = 0
    demoted: int = 0
    expiring: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"retained": self.retained, "demoted": self.demoted, "expiring": self.expiring}


def select(
    rows: list[dict[str, Any]], *, promoted_keys: set[str]
) -> tuple[list[dict[str, Any]], int]:
    """Pure: which of the run's live rows the retention acts on, and how many
    it retains. A row is retained when pinned, verified, promoted (a live
    shared row shares its key), not a fact type, or not active."""

    act: list[dict[str, Any]] = []
    retained = 0
    for r in rows:
        if (
            bool(r.get("pinned"))
            or r.get("verified_at") is not None
            or str(r.get("memory_key") or "") in promoted_keys
            or str(r.get("memory_type") or "") in _NOT_FACTS
            or str(r.get("status") or "active") not in ("active", "conflicted")
            or not r.get("memory_id")
        ):
            retained += 1
            continue
        act.append(r)
    return act, retained


def demoted_confidence(conf: float, *, factor: float, floor: float) -> float:
    return round(max(floor, min(1.0, float(conf or 0.0) * factor)), 4)


async def load_run_rows(
    ch: Any, *, org: str, namespace: str, run_id: str, limit: int
) -> list[dict[str, Any]]:
    """The run's live rows with the columns `select` and the mirror need.
    Locked rows are excluded in SQL (the lock refuses the value / expiry
    write) and count as retained by the caller."""

    from server.identity.app.request_scope import request_scope
    from server.identity.domain.read_scope import scope_and
    from server.memory.domain.lock import locked_predicate

    params: dict[str, Any] = {"org": org, "ns": namespace, "rid": run_id, "lim": int(limit)}
    # A run id is the caller's word: only rows inside its read scope are retained.
    in_scope = scope_and(request_scope(), params)
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS memory_id, memory_key, memory_type, agent, user_id,
               session_id, pinned, verified_at, status, confidence, expires_at,
               {locked_predicate()} AS locked
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND run_id = {{rid:String}} AND tombstoned_at IS NULL{in_scope}
        ORDER BY memory_key
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return rows


async def promoted_keys_of(ch: Any, *, org: str, namespace: str, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = await ch.query_all(
        """
        SELECT DISTINCT memory_key FROM loom.shared FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
        """,
        params={"org": org, "ns": namespace, "keys": sorted(set(keys))},
    )
    return {str(r["memory_key"]) for r in rows}


async def apply(
    *,
    org: str,
    namespace: str,
    run_id: str,
    mode: str,
    ch: Any,
    now: datetime | None = None,
    demote_factor: float,
    demote_floor: float,
    expire_days: int,
    limit: int = 1000,
) -> Counts:
    """Apply the retention to the run's rows. Fail-open per run: an error
    logs and returns what was counted so far; run.complete never fails on it."""

    counts = Counts()
    if mode not in ("demote", "expire") or not run_id or run_id == "-":
        return counts
    import server.memory.store as memory_repo
    from server.memory.app.apply import _mirror

    now = now or naive_utc_now()
    try:
        rows = await load_run_rows(ch, org=org, namespace=namespace, run_id=run_id, limit=limit)
        unlocked = [r for r in rows if not bool(r.get("locked"))]
        counts.retained += len(rows) - len(unlocked)
        promoted = await promoted_keys_of(
            ch,
            org=org,
            namespace=namespace,
            keys=[str(r.get("memory_key") or "") for r in unlocked],
        )
        act, retained = select(unlocked, promoted_keys=promoted)
        counts.retained += retained
        if not act:
            return counts
        expires_at = now + timedelta(days=int(expire_days))
        reason = f"run {run_id} completed (retain={mode})"
        revised: list[dict[str, Any]] = []

        def _update(existing: Any) -> dict[str, Any]:
            # Drawn after the row is read, so a write that landed before the
            # read sorts below this version.
            version = memory_repo.next_version()
            revised.append(memory_change(existing, version))
            if mode == "demote":
                return {
                    "confidence": demoted_confidence(
                        float(getattr(existing, "confidence", 0.0) or 0.0),
                        factor=demote_factor,
                        floor=demote_floor,
                    ),
                    "version": version,
                }
            return {
                "expires_at": expires_at,
                "on_expire": "demote",
                "expire_reason": reason,
                "version": version,
            }

        n = await memory_repo.write_versions_by_ids(
            org,
            [str(r["memory_id"]) for r in act],
            _update,
            namespace=namespace,
            ch=ch,
        )
        await webhooks.announce([("memory.revised", change) for change in revised])
        # The embeddings row is what search reads: mirror the same change.
        for r in act:
            row = _RowShim(
                org=org,
                namespace=namespace,
                **{
                    k: str(r.get(k) or "") for k in ("agent", "user_id", "session_id", "memory_key")
                },
            )
            if mode == "demote":
                conf = demoted_confidence(
                    float(r.get("confidence") or 0.0), factor=demote_factor, floor=demote_floor
                )
                await _mirror(row, confidence=conf)
            else:
                await _mirror(row, expires_at=expires_at)
        if mode == "demote":
            counts.demoted = n
        else:
            counts.expiring = n
        log.info(
            "run retention (%s): %d rows acted on, %d retained for run %s in %s/%s",
            mode,
            n,
            counts.retained,
            run_id,
            org,
            namespace,
        )
    except Exception as e:  # noqa: BLE001 — the run row is the contract; retention is best effort
        log.warning("run retention failed (non-fatal) for run %s: %s", run_id, e)
    return counts


class _RowShim:
    """The identity fields memory_apply._mirror reads off a MemoryRow."""

    def __init__(self, **fields: str) -> None:
        for k, v in fields.items():
            setattr(self, k, v)
