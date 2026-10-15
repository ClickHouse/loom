"""Each subject's grant rows on an org, cached per process for the request path.

A token's claim on a namespace stands only where its subject holds no grant
row there; where it holds one, the claim is capped at that grant
(`authz.claim_capped`). That needs the subject's rows on every request, so they
are read once per (org, spellings) and kept for `grant_cache_ttl_seconds`.

A write in this process drops the entries it touches at once; another process
sees it within the TTL. A failed re-read serves the old entry while it is
younger than `jwt_revocation_max_stale_seconds`, the bound a token check already
lives with, and refuses (503) past it.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from server.core.errors import BackendUnavailableError
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT
from server.settings import settings

_MAX_ENTRIES = 10_000


@dataclass(frozen=True, slots=True)
class GrantMap:
    """One subject's grants on one org: every namespace it has a row on, of
    any status, and the highest live role on each (0 when none is live)."""

    ranks: dict[str, int]

    def has_row(self, namespace: str) -> bool:
        return namespace in self.ranks


_Key = tuple[str, frozenset[str]]
_entries: OrderedDict[_Key, tuple[float, GrantMap]] = OrderedDict()
_loading: dict[_Key, asyncio.Future[GrantMap]] = {}


async def grant_map(org: str, subjects: Iterable[str]) -> GrantMap:
    """The grants `subjects` (one person's spellings) hold on `org`."""
    key: _Key = (org, frozenset(s.lower() for s in subjects))
    hit = _entries.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < settings.grant_cache_ttl_seconds:
        _entries.move_to_end(key)
        return hit[1]
    pending = _loading.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    future: asyncio.Future[GrantMap] = asyncio.get_running_loop().create_future()
    _loading[key] = future
    try:
        try:
            fresh = await _read(org, sorted(key[1]))
        except Exception as e:
            if hit is not None and now - hit[0] < settings.jwt_revocation_max_stale_seconds:
                fresh = hit[1]
            else:
                raise BackendUnavailableError(
                    "grants could not be read; retry shortly", operation="grant_map"
                ) from e
        else:
            _store(key, fresh)
        future.set_result(fresh)
        return fresh
    except BaseException as e:
        future.set_exception(e)
        future.exception()  # read here, so a waiter-less failure is not logged as lost
        raise
    finally:
        del _loading[key]


def invalidate(org: str, subjects: Iterable[str]) -> None:
    """Drop the cached grants of any spelling set holding one of `subjects`."""
    lowered = {s.lower() for s in subjects}
    for key in [k for k in _entries if k[0] == org and k[1] & lowered]:
        del _entries[key]


def clear() -> None:
    _entries.clear()


def _store(key: _Key, value: GrantMap) -> None:
    _entries[key] = (time.monotonic(), value)
    _entries.move_to_end(key)
    while len(_entries) > _MAX_ENTRIES:
        _entries.popitem(last=False)


async def _read(org: str, subjects: list[str]) -> GrantMap:
    from server.core.ch import get_client, own_write_reads

    with own_write_reads():
        rows = await get_client().query_all(
            """
            SELECT namespace, max(if({live}, {rank}, 0)) AS rank
            FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND lower(subject) IN {subs:Array(String)}
            GROUP BY namespace
            """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
            params={"org": org, "subs": subjects},
        )
    return GrantMap(ranks={str(r["namespace"]): int(r["rank"]) for r in rows})
