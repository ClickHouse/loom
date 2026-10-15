"""Each subject's grant rows on an org, cached per process for the request path.

A token's claim on a namespace stands only where its subject holds no grant
row there; where it holds one, the claim is capped at that grant
(`authz.claim_cap`). That needs the subject's rows on every request, so one
read per (org, subject), every spelling of the person included, is kept for
`grant_cache_ttl_seconds`.

A grant write in this process drops its org's entries at once (a writer knows
the spelling it wrote, not the ones a token may carry); another process sees it
within the TTL. A miss reads the org's grant rows, so a busy org pays one such
read per active subject per TTL. A failed re-read serves the old entry while it is
younger than `jwt_revocation_max_stale_seconds`, the bound a token check already
lives with, and refuses (503) past it.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from server.core.errors import BackendUnavailableError
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT
from server.settings import settings

_MAX_ENTRIES = 10_000


@dataclass(frozen=True, slots=True)
class GrantMap:
    """One subject's grants on one org: every namespace it has a row on, of
    any status, and the highest live grant rank there (0 when none is live)."""

    ranks: dict[str, int]

    def rank(self, namespace: str) -> int | None:
        return self.ranks.get(namespace)


_Key = tuple[str, str]
_entries: OrderedDict[_Key, tuple[float, GrantMap]] = OrderedDict()
_loading: dict[_Key, asyncio.Task[GrantMap]] = {}
# Every read until it ends: an invalidation drops it from `_loading`, not from here.
_running: set[asyncio.Task[GrantMap]] = set()
# Bumped by every invalidation, so a read that began before it is not stored.
_generation: defaultdict[str, int] = defaultdict(int)


async def grant_map(org: str, subject: str) -> GrantMap:
    """The grants `subject`, under any of its spellings, holds on `org`."""
    key: _Key = (org, subject.lower())
    hit = _entries.get(key)
    if hit is not None and time.monotonic() - hit[0] < settings.grant_cache_ttl_seconds:
        _entries.move_to_end(key)
        return hit[1]
    task = _loading.get(key)
    if task is None:
        # A task of its own, so a cancelled request does not fail the ones waiting with it.
        task = asyncio.ensure_future(_load(key, hit))
        _loading[key] = task
        _running.add(task)
        task.add_done_callback(lambda t: _loaded(key, t))
    return await asyncio.shield(task)


def invalidate(org: str) -> None:
    """Drop every cached grant of `org`, and any read of it in flight."""
    _generation[org] += 1
    for key in [k for k in _entries if k[0] == org]:
        del _entries[key]
    for key in [k for k in _loading if k[0] == org]:
        del _loading[key]


def clear() -> None:
    _entries.clear()
    _loading.clear()


async def _load(key: _Key, hit: tuple[float, GrantMap] | None) -> GrantMap:
    org = key[0]
    generation = _generation[org]
    try:
        fresh = await _read(org, key[1])
    except Exception as e:
        if (
            hit is not None
            and time.monotonic() - hit[0] < settings.jwt_revocation_max_stale_seconds
        ):
            return hit[1]
        raise BackendUnavailableError(
            "grants could not be read; retry shortly", operation="grant_map"
        ) from e
    if _generation[org] == generation:
        _entries[key] = (time.monotonic(), fresh)
        _entries.move_to_end(key)
        while len(_entries) > _MAX_ENTRIES:
            _entries.popitem(last=False)
    return fresh


def _loaded(key: _Key, task: asyncio.Task[GrantMap]) -> None:
    _running.discard(task)
    if _loading.get(key) is task:
        del _loading[key]
    if not task.cancelled():
        task.exception()  # read here, so a failure nobody awaited is not logged as lost


async def _read(org: str, subject: str) -> GrantMap:
    """One read: the subject's rows under every spelling the directory ties to
    it (`server.auth.aliases`), folded per namespace. A deactivated account
    still ties its spellings, and so does a released one (`released_aliases`):
    its grants there may be the revocations that cap."""
    from server.core.ch import get_client, own_write_reads

    human = bool(subject) and subject != "*" and not subject.startswith("sa:")
    with own_write_reads():
        rows = await get_client().query_all(
            """
            SELECT namespace, max(if({live}, {rank}, 0)) AS rank
            FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND lower(subject) IN (
                SELECT {sub:String}
                UNION ALL
                SELECT lower(arrayJoin([owner_email, contact_email]))
                FROM loom.namespaces FINAL
                WHERE {human:Bool} AND org = {org:String} AND contact_email != ''
                  AND (lower(owner_email) = {sub:String} OR lower(contact_email) = {sub:String})
                UNION ALL
                SELECT lower(arrayJoin(released_aliases))
                FROM loom.namespaces FINAL
                WHERE {human:Bool} AND org = {org:String}
                  AND has(arrayMap(x -> lower(x), released_aliases), {sub:String})
            )
            GROUP BY namespace
            """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
            params={"org": org, "sub": subject, "human": human},
        )
    return GrantMap(ranks={str(r["namespace"]): int(r["rank"]) for r in rows})
