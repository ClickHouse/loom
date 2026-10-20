"""org.set_model_calls / org.model_calls handlers."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from server.identity.api.models_model_calls import (
    OrgModelCallsInput,
    OrgModelCallsOutput,
    OrgSetModelCallsInput,
)
from server.identity.store import model_calls as repo


async def handle_org_set_model_calls(input: OrgSetModelCallsInput) -> OrgModelCallsOutput:
    """Switch the org's model calls off or on. This process sees it on its
    next call; every other within `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`."""
    from server.auth.context import current_auth
    from server.llm import customer

    auth = current_auth()
    now = datetime.now(UTC).replace(tzinfo=None)
    by = auth.subject if auth is not None and auth.subject else "admin"
    await repo.insert_row(
        {
            "org": input.org,
            "enabled": int(input.enabled),
            "reason": input.reason,
            "updated_by": by,
            "updated_at": now,
            "version": time.time_ns(),
        }
    )
    customer.forget_switch()
    return OrgModelCallsOutput(
        org=input.org,
        enabled=input.enabled,
        reason=input.reason,
        updated_by=by,
        updated_at=now,
    )


async def handle_org_model_calls(input: OrgModelCallsInput) -> OrgModelCallsOutput:
    """The org's current setting; enabled when it was never set."""
    row = await repo.get(input.org)
    if row is None:
        return OrgModelCallsOutput(org=input.org, enabled=True)
    return OrgModelCallsOutput(
        org=input.org,
        enabled=bool(row["enabled"]),
        reason=str(row["reason"] or ""),
        updated_by=str(row["updated_by"] or ""),
        updated_at=row["updated_at"],
    )
