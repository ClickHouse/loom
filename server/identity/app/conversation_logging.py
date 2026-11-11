"""The per-namespace conversation-logging switch on the namespace directory row."""

from __future__ import annotations

from server.auth.grant_claims import account_change
from server.core.datetimes import naive_utc_now_seconds
from server.identity.store import namespaces as namespaces_repo


async def set_conversation_logging(org: str, namespace: str, enabled: bool) -> None:
    """Write a new `loom.namespaces` version row, the same shape as
    namespace.delete: the live row copied forward with the switch flipped, so
    FINAL readers see it at once without a synchronous mutation. A namespace
    written to but never registered has no row, so one is created: a mutation
    there would match nothing and the Record conversations switch would report
    On for a namespace that never records."""
    from server.core.ch import get_client

    ch = get_client()
    async with account_change(org, namespace):  # a release must not be written back over
        existing = await namespaces_repo.fetch_row(org, namespace, ch=ch)
        if existing is None:
            from server.identity.app.namespace import refuse_sso_name

            refuse_sso_name(namespace)
        now = naive_utc_now_seconds()
        row = (
            dict(existing)
            if existing is not None
            else {
                "org": org,
                "namespace": namespace,
                "active": True,
                "visibility": "private",
                "created_at": now,
            }
        )
        row["conversation_logging_enabled"] = enabled
        row["updated_at"] = now
        await ch.insert("loom.namespaces", [row])
