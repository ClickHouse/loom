"""org.set_model_calls / org.model_calls handlers, and `switch_off` for the
paths that switch an org off themselves (offboarding)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from server.core.errors import AuthInsufficientError, ValidationError
from server.identity.api.models_model_calls import (
    OrgModelCallsInput,
    OrgModelCallsOutput,
    OrgSetModelCallsInput,
)
from server.identity.store import model_calls as repo


def _one_org(org: str) -> None:
    # "", "-" and "*" name no org (the identity tuple's none, a wildcard).
    if org.strip() in ("", "-", "*") or org != org.strip():
        raise ValidationError(f"org must name one org, got {org!r}")


def _caller() -> tuple[str, bool]:
    """(the caller's subject, whether it is a platform admin). With auth off
    (development) there is no caller, and nothing to lock against."""
    from server.auth.context import current_auth
    from server.operations.base import Role

    auth = current_auth()
    if auth is None:
        return "admin", True
    return auth.subject or "admin", auth.has_role(Role.PLATFORM_ADMIN)


async def handle_org_set_model_calls(input: OrgSetModelCallsInput) -> OrgModelCallsOutput:
    """Switch the org's model calls off or on. A platform admin may also lock
    or unlock the setting; while it is locked, the org's own admins are refused.
    Every process sees a change within `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`."""
    _one_org(input.org)
    by, platform = _caller()
    if input.locked is not None and not platform:
        raise AuthInsufficientError("only a platform admin locks or unlocks model calls")
    return await _set(input.org, input.enabled, input.reason, by, input.locked, platform)


async def switch_off(org: str, *, by: str, reason: str, locked: bool = True) -> None:
    """Switch the org's model calls off, locked against its own admins unless
    `locked` is false: for a path that ends an org's use (offboarding), which
    acts with the platform's authority."""
    _one_org(org)
    await _set(org, False, reason, by, locked, True)


async def _set(
    org: str, enabled: bool, reason: str, by: str, locked: bool | None, platform: bool
) -> OrgModelCallsOutput:
    from server.auth.grant_claims import model_calls_change
    from server.llm import customer

    async with model_calls_change(org):
        current = await repo.get(org) or {"locked": 0, "locked_by": ""}
        held = bool(current["locked"])
        if held and not platform:
            raise AuthInsufficientError(
                f"model calls of {org!r} are locked by {current['locked_by']!r}: "
                "only a platform admin changes them"
            )
        lock = held if locked is None else locked
        locked_by = str(current["locked_by"]) if locked is None else by
        now = datetime.now(UTC).replace(tzinfo=None)
        row: dict[str, Any] = {
            "org": org,
            "enabled": int(enabled),
            "reason": reason,
            "updated_by": by,
            "updated_at": now,
            "locked": int(lock),
            "locked_by": locked_by if lock else "",
            "version": time.time_ns(),
        }
        await repo.insert_row(row)
    customer.forget_switch()
    return _output(org, row)


async def handle_org_model_calls(input: OrgModelCallsInput) -> OrgModelCallsOutput:
    """The org's current setting; enabled and unlocked when it was never set."""
    _one_org(input.org)
    row = await repo.get(input.org)
    if row is None:
        return OrgModelCallsOutput(org=input.org, enabled=True)
    return _output(input.org, row)


def _output(org: str, row: dict[str, Any]) -> OrgModelCallsOutput:
    return OrgModelCallsOutput(
        org=org,
        enabled=bool(row["enabled"]),
        reason=str(row["reason"] or ""),
        updated_by=str(row["updated_by"] or ""),
        updated_at=row["updated_at"],
        locked=bool(row["locked"]),
        locked_by=str(row["locked_by"] or ""),
    )
