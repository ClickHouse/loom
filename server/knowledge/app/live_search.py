"""Live search owns private work until cancellation or final publication."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.auth import session_admission
from server.auth.session_protection import protected_for
from server.core import request_tail, request_work
from server.knowledge.app import connected_accounts, live_provider_search
from server.knowledge.domain.sources import MIRRORED_ACCESS
from server.knowledge.store import mirror_latch, source_store
from server.settings import settings

if TYPE_CHECKING:
    from server.auth.credentials import Principal
    from server.knowledge.app.connected_accounts import CheckedAccess


@asynccontextmanager
async def private_work() -> AsyncIterator[None]:
    """Mirror protection must mark every thread or task that touches cleartext work."""
    with request_work.mirror_scope(), request_work.http_mirror_scope():
        yield


@dataclass(frozen=True)
class Contribution:
    authority: connected_accounts.SourceAuthority
    hits: tuple[live_provider_search.LiveProviderHit, ...]

    async def check(self, *, deadline: float) -> None:
        async def native(access: CheckedAccess) -> None:
            tasks = [
                live_provider_search.revalidate(
                    self.authority.source, access, hit, deadline=deadline
                )
                for hit in self.hits
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for hit, result in zip(self.hits, results, strict=True):
                if isinstance(result, Exception) or not result:
                    raise PermissionError(f"Live search hit revoked: {hit.id}")

        await self.authority.run(native)


@dataclass(frozen=True)
class LiveResult:
    contributions: tuple[Contribution, ...]
    deadline: float

    async def check(self) -> None:
        for contribution in self.contributions:
            await contribution.check(deadline=self.deadline)

    def fresh(self) -> bool:
        return time.monotonic() < self.deadline


async def search(
    org: str,
    namespaces: Sequence[str],
    query: str,
    *,
    top_k: int,
) -> LiveResult | None:
    """Live search keeps provider calls separate from indexed search and each other."""
    if not (
        settings.knowledge_mirrored_sources_enabled
        and settings.auth_required
        and request_tail.required_active()
        and request_work.mirror_marked()
        and request_work.http_mirror_marked()
    ):
        return None

    deadline = time.monotonic() + settings.knowledge_live_search_timeout_s
    with private_work():
        try:
            async with mirror_latch.ensure():
                auth: Principal = request_tail.current_principal()
                if not protected_for(auth):
                    return None
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
                    and source.kind in ("gdrive", "notion")
                ][: settings.knowledge_live_search_max_sources]
                if not selected:
                    return None
                # Real opt-in work activates the same durable protection as mirrored sources.
                await session_admission.require(auth)
                contributions = []
                total_budget = min(top_k, settings.knowledge_live_search_max_hits)
                num_sources = len(selected)

                for i, source in enumerate(selected):
                    if total_budget <= 0:
                        break

                    sources_left = num_sources - i
                    source_quota = min(total_budget, math.ceil(total_budget / sources_left))

                    try:
                        authority = await connected_accounts.resolve(
                            org,
                            source.namespace,
                            source.name,
                            deadline=deadline,
                            expected_source_id=source.source_id,
                        )
                        if authority.source != source:
                            continue  # a changed registration cannot widen the discovered corpus

                        async def native(
                            access: CheckedAccess,
                            authority: connected_accounts.SourceAuthority = authority,
                            source_quota: int = source_quota,
                        ) -> list[live_provider_search.LiveProviderHit]:
                            return await live_provider_search.search(
                                authority.source, access, query, top_k=source_quota, deadline=deadline
                            )

                        async with asyncio.timeout_at(deadline):
                            hits = tuple(await authority.run(native))[:source_quota]

                        if hits:
                            contributions.append(Contribution(authority, hits))
                            total_budget -= len(hits)
                    except Exception:  # noqa: BLE001 - timeout or provider failure never drops prior contributions
                        if time.monotonic() >= deadline:
                            break
                        continue

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
