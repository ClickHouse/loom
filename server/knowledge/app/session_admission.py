"""Global admission for clients that send session content automatically."""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.auth.session_protection import protected_for
from server.core.errors import SessionProtectionRequiredError
from server.knowledge.store import mirror_latch
from server.settings import settings

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo
    from server.core.ch import CHClient


async def reassert_local_switch(ch: CHClient) -> None:
    """A process observes its on switch only with durable protection in place."""
    if settings.knowledge_mirrored_sources_enabled:
        await mirror_latch.ensure(ch=ch)


async def require(auth: AuthInfo | None) -> None:
    """A protection promise or a fresh deployment absence admits session content."""
    declared = protected_for(auth)
    try:
        if settings.knowledge_mirrored_sources_enabled:
            await mirror_latch.ensure()
            if declared:
                return
        elif declared or not await mirror_latch.enabled():
            return
    except mirror_latch.LatchUnavailable:
        pass
    raise SessionProtectionRequiredError() from None
