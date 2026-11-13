"""The identity reads behind an MCP caller's default namespace.

`caller_home` decides which namespace an unaddressed call lands in; what it
ranks — the user's saved dashboard pick and their live grants — is read here
through the identity store.
"""

from __future__ import annotations

from typing import Any

from server.identity.store import grants as grants_store
from server.identity.store import user_state as user_state_store


async def saved_default(org: str, subject: str, *, ch: Any | None = None) -> str:
    """The namespace `subject` picked in the dashboard's namespace picker, "" when none."""
    return await user_state_store.saved_default_namespace(org, subject, ch=ch)


async def ranked_grants(org: str, subjects: list[str], *, ch: Any | None = None) -> list[Any]:
    """`subjects`' live grants in `org`, highest role then most recent first."""
    return await grants_store.ranked_live_grants(org, subjects, ch=ch)


__all__ = ["ranked_grants", "saved_default"]
