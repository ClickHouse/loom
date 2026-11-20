"""Private canonical-owner verification; it does not widen end-user erasure."""

from server.core.errors import OverloadedError
from server.core.repo_base import _CHClient
from server.erasure.store.census import connected_account_residue, connected_source_residue


async def require_erased(
    org: str,
    subject: str,
    kind: str,
    *,
    keep_connection: str | None = None,
    ch: _CHClient | None = None,
) -> None:
    if await connected_account_residue(org, subject, kind, keep_connection=keep_connection, ch=ch):
        raise OverloadedError("connected-account cleanup is incomplete; retry", retry_after_s=1)


async def require_sources_erased(
    org: str, namespace: str, subject: str, kind: str, *, ch: _CHClient | None = None
) -> None:
    if await connected_source_residue(org, namespace, subject, kind, ch=ch):
        raise OverloadedError("connected-source cleanup is incomplete; retry", retry_after_s=1)


async def require_restore_erased(*, ch: _CHClient | None = None) -> None:
    from server.erasure.store.census import connected_restore_residue

    if await connected_restore_residue(ch=ch):
        raise OverloadedError(
            "connected-account restore cleanup is incomplete; retry", retry_after_s=1
        )
