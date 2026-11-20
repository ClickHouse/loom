"""Required downstream cleanup after canonical sign-in authority is revoked.

HTTP and CLI composition bind the knowledge implementation. Auth never imports a
business domain, and an uncomposed process refuses instead of skipping retirement.
"""

from __future__ import annotations

from typing import Protocol

from server.core.errors import BackendUnavailableError


class ConnectionStopper(Protocol):
    async def __call__(
        self,
        subjects: list[str],
        *,
        revoke: bool = True,
        erase_owner: bool = False,
        respect_cutoff: bool = False,
    ) -> None: ...


_stopper: ConnectionStopper | None = None


def bind(stopper: ConnectionStopper) -> None:
    """Compose the real lifecycle before accepting HTTP or executing a CLI command."""
    global _stopper
    _stopper = stopper


async def stop_connections(
    subjects: list[str],
    *,
    revoke: bool = True,
    erase_owner: bool = False,
    respect_cutoff: bool = False,
) -> None:
    if _stopper is None:
        raise BackendUnavailableError(
            "connected-account lifecycle is not initialized", operation="connected_accounts"
        )
    await _stopper(subjects, revoke=revoke, erase_owner=erase_owner, respect_cutoff=respect_cutoff)
