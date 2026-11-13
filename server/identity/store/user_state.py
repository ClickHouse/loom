"""Repository for loom.user_state: a dashboard user's saved choices."""

from __future__ import annotations

from server.core.repo_base import _CHClient, _default_client


async def saved_default_namespace(org: str, subject: str, *, ch: _CHClient | None = None) -> str:
    """The namespace the user picked in the dashboard's namespace picker, "" when none."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT default_namespace AS namespace
        FROM (SELECT * FROM loom.user_state FINAL) AS u
        WHERE u.org = {org:String} AND u.subject = {sub:String}
        LIMIT 1
        """,
        params={"org": org, "sub": subject},
    )
    return str((row or {}).get("namespace") or "")
