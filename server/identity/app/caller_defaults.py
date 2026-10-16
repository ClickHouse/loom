"""The identity reads behind an MCP caller's default namespace.

The transport decides which namespace an unaddressed call lands in
(`server/transport/mcp_identity.py`); what it decides on — the user's saved
dashboard pick and their live grants, ranked — is the identity plane's data, read
here through its store.
"""

from __future__ import annotations

from typing import Any

from server.identity.store import grants as grants_store
from server.identity.store import user_state as user_state_store


async def saved_default(org: str, subject: str, *, ch: Any | None = None) -> str:
    """The namespace `subject` picked in the dashboard's switcher, "" when none."""
    return await user_state_store.saved_default_namespace(org, subject, ch=ch)


async def live_role_on(
    org: str, namespace: str, subjects: list[str], *, ch: Any | None = None
) -> str:
    """The highest live grant role of any of `subjects` on the namespace."""
    return await grants_store.best_live_role(org, namespace, subjects, ch=ch)


async def ranked_grants(org: str, subjects: list[str], *, ch: Any | None = None) -> list[Any]:
    """`subjects`' live grants in `org`, highest role then most recent first."""
    return await grants_store.ranked_live_grants(org, subjects, ch=ch)


__all__ = ["live_role_on", "ranked_grants", "saved_default"]
