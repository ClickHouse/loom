"""Request-owned MCP lookup over the asker's selected live registrations."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from server.auth.answer_tokens import is_answer_token
from server.auth.context import current_auth
from server.auth.session_protection import protected_for
from server.core import request_tail, request_work
from server.core.ch import set_query_tag
from server.core.diagnostics import private_work
from server.knowledge.app import connected_accounts, mcp_live, session_admission
from server.knowledge.domain.sources import MIRRORED_ACCESS
from server.knowledge.store import sources as source_store
from server.settings import settings


@dataclass(repr=False)
class LiveResult:
    contributions: tuple[mcp_live.Contribution, ...]
    deadline: float
    _checked: bool = False

    @property
    def hits(self) -> tuple[mcp_live.LiveHit, ...]:
        return tuple(hit for part in self.contributions for hit in part.hits)

    async def check(self) -> None:
        """Recheck current authority and each retained successful selected read."""
        self._checked = False
        with private_work():
            async with asyncio.timeout_at(self.deadline):
                for part in self.contributions:
                    await part.check()
                self._checked = True

    def fresh(self) -> bool:
        return self._checked and time.monotonic() < self.deadline


async def search(org: str, namespaces: list[str], query: str, *, top_k: int) -> LiveResult | None:
    """Unavailable private work contributes no section and exposes no error detail."""
    auth = current_auth()
    if (
        auth is None
        or auth.org != org
        or not settings.auth_actually_required()
        or not settings.knowledge_mirrored_sources_enabled
        or not protected_for(auth)
        or (is_answer_token(auth) and not auth.answer_mirrors)
        or not request_tail.required_active()
        or len(query) > 1000
    ):
        return None
    if not request_work.mark_mirrored():
        return None
    deadline = time.monotonic() + settings.knowledge_live_search_timeout_s
    set_query_tag("search.query.live")
    with private_work():
        try:
            async with asyncio.timeout_at(deadline):
                actual_org, subject = await connected_accounts.own_subject(auth)
                if actual_org != org:
                    return None
                sources = await source_store.connected_sources(org, subject)
                selected = [
                    source
                    for source in sources
                    if source.namespace in namespaces
                    and source.active
                    and source.credential_mode == "connected"
                    and source.owner_subject == subject
                    and source.access == MIRRORED_ACCESS
                    and mcp_live.is_live_source(source)
                ][: settings.knowledge_live_search_max_sources]
                if not selected:
                    return None
                # Real opt-in work activates the same durable protection as mirrored sources.
                await session_admission.require(auth)
                contributions = []
                remaining = min(top_k, settings.knowledge_live_search_max_hits)
                for source in selected:
                    part = await mcp_live.search(source, auth, query, limit=remaining)
                    if part is not None:
                        contributions.append(part)
                        remaining -= len(part.hits)
                    if not remaining:
                        break
                return LiveResult(tuple(contributions), deadline) if contributions else None
        except Exception:  # noqa: BLE001 — optional private search never exposes account/provider detail
            return None


async def publish(result: LiveResult | None) -> LiveResult | None:
    """Only selected, current contributions retain a final-response obligation."""
    if result is None:
        return None
    with private_work():
        try:
            await result.check()
            if not result.fresh():
                return None
            if not request_tail.retain_publication(result, result.check, result.fresh):
                return None
            return result
        except Exception:  # noqa: BLE001 — expired or revoked live results leave indexed search intact
            return None
