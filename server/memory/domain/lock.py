"""A lock on a memory row is a write-time invariant.

Two modes, each naming the write classes it refuses:

    read_only  — value, expiry and deletion are all refused
    no_delete  — the value may change; tombstone / redact / clear /
                 consolidate-away / reject are refused

Every write handler fetches the head row before it writes; `assert_unlocked`
reads the lock columns off that same row, so the check costs no extra
round trip. A lapsed lock (`lock_until` in the past) is no lock. Refusals
are a structured 423 naming the mode, the reason, who locked it and how it
comes off — the same shape as the policy gate's 403 — so an agent can
report the refusal instead of retrying it.

There is deliberately no `append_only` mode: a loom row has no append surface
of its own (tags, links, outcomes and attestations are metadata ops that no
lock gates), so it would collapse into `read_only`. Locking is a WRITER action
and loosening an ADMIN action: narrowing access is silent, widening it is
explicit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException

from server.core.datetimes import naive_utc_now as _now
from server.core.datetimes import to_naive_utc as _naive

LockAction = Literal["value", "expire", "delete"]

# Which write classes each mode refuses. A mode refusing a superset of
# actions is stricter.
REFUSES: dict[str, frozenset[str]] = {
    "read_only": frozenset({"value", "expire", "delete"}),
    "no_delete": frozenset({"delete"}),
}

UNLOCK_VIA = "memory.unlock (ADMIN role)"


def effective_mode(row: Any, *, now: datetime | None = None) -> str:
    """The lock mode in force on `row` right now ('' when unlocked or lapsed)."""
    mode = str(getattr(row, "lock_mode", "") or "")
    if mode not in REFUSES:
        return ""
    until = _naive(getattr(row, "lock_until", None))
    if until is not None and until <= (now or _now()):
        return ""
    return mode


def refuses_retirement(row: Any) -> bool:
    """A row another write may not supersede (the refines / newest_wins /
    repair retirements): both modes refuse it — `read_only` refuses any
    change, `no_delete` refuses consolidate-away."""
    return bool(effective_mode(row))


def is_stricter_or_equal(new_mode: str, old_mode: str) -> bool:
    """`new_mode` refuses everything `old_mode` refuses."""
    return REFUSES.get(new_mode, frozenset()) >= REFUSES.get(old_mode, frozenset())


def lock_state(row: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-side projection of a row's lock, for get / list outputs."""
    mode = effective_mode(row, now=now)
    if not mode:
        return {"lock_mode": "", "lock_reason": "", "lock_until": None, "locked_by": ""}
    return {
        "lock_mode": mode,
        "lock_reason": str(getattr(row, "lock_reason", "") or ""),
        "lock_until": _naive(getattr(row, "lock_until", None)),
        "locked_by": str(getattr(row, "locked_by", "") or ""),
    }


def refusal(row: Any, action: str, *, op: str, mode: str) -> HTTPException:
    until = _naive(getattr(row, "lock_until", None))
    reason = str(getattr(row, "lock_reason", "") or "")
    by = str(getattr(row, "locked_by", "") or "")
    key = str(getattr(row, "memory_key", "") or "")
    return HTTPException(
        status_code=423,
        detail={
            "code": "LOCKED",
            "message": (
                f"{op} refused: memory {key!r} is locked {mode} "
                f"({reason or 'no reason recorded'})"
                + (f" by {by}" if by else "")
                + (f" until {until.isoformat()}Z" if until is not None else "")
                # Say who can act and what the caller does
                # now. memory.unlock is hidden from MCP by design (loosening
                # a lock is the owner's act), so an agent
                # is told to leave the key, not to call an op it cannot reach.
                # Name the person, never a route — an agent
                # told of a route goes looking for credentials to call it.
                + ". Only the user can unlock it: "
                "leave the key as it is and tell them it is locked. Don't look for "
                "another route, token or credential to unlock it yourself."
            ),
            "memory_key": key,
            "memory_id": str(getattr(row, "memory_id", "") or ""),
            "lock_mode": mode,
            "lock_reason": reason,
            "locked_by": by,
            "lock_until": until.isoformat() if until is not None else None,
            "refused_action": action,
            "unlock_via": UNLOCK_VIA,
        },
    )


def assert_unlocked(row: Any, action: LockAction, *, op: str, now: datetime | None = None) -> None:
    """Raise 423 LOCKED when the lock in force on `row` refuses `action`."""
    if row is None:
        return
    mode = effective_mode(row, now=now)
    if mode and action in REFUSES[mode]:
        raise refusal(row, action, op=op, mode=mode)


def locked_predicate(alias: str = "", now: str = "now()") -> str:
    """SQL: the row carries a lock that is in force at `now`. `alias` prefixes columns.
    A mutation passes a bound parameter: a replicated table refuses now() in one."""
    a = f"{alias}." if alias else ""
    return f"({a}lock_mode != '' AND ({a}lock_until IS NULL OR {a}lock_until > {now}))"


def unlocked_predicate(alias: str = "", now: str = "now()") -> str:
    a = f"{alias}." if alias else ""
    return f"({a}lock_mode = '' OR ({a}lock_until IS NOT NULL AND {a}lock_until <= {now}))"
