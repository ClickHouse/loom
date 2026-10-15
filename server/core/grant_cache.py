"""Each subject's grant rows on an org, cached per process for the request path.

A token's claim on a namespace stands only where its subject holds no grant
row there; where it holds one, the claim is capped at that grant
(`authz.claim_cap`). That needs the subject's rows on every request, so one
read per (org, subject), every spelling of the person included, is kept for
`grant_cache_ttl_seconds`.

A grant write in this process drops the entries whose read covered the
spelling it wrote, at once; a write to the accounts that tie a person's
spellings drops its org's. This server's other web processes read again at
once too: the write marks a file they share, and an entry read before the mark
is not served. Another server sees either within the TTL. A failed
re-read serves the old entry while it is younger than
`jwt_revocation_max_stale_seconds`, the bound a token check already lives with,
and refuses (503) past it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from server.core.errors import BackendUnavailableError
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT
from server.settings import settings

log = logging.getLogger(__name__)

_MAX_ENTRIES = 10_000


@dataclass(frozen=True, slots=True)
class GrantMap:
    """One subject's grants on one org: every namespace it has a row on, of
    any status, and the highest live grant rank there (0 when none is live);
    and the spellings the read covered."""

    ranks: dict[str, int]
    spellings: frozenset[str] = frozenset()

    def rank(self, namespace: str) -> int | None:
        return self.ranks.get(namespace)


_Key = tuple[str, str]
# (when stored, monotonic; when its read began, wall-clock ns; the grants)
_Entry = tuple[float, int, GrantMap]
_entries: OrderedDict[_Key, _Entry] = OrderedDict()
_loading: dict[_Key, asyncio.Task[GrantMap]] = {}
# Every read until it ends: an invalidation drops it from `_loading`, not from here.
_running: set[asyncio.Task[GrantMap]] = set()
# Bumped by every invalidation, so a read that began before it is not stored.
_generation: defaultdict[str, int] = defaultdict(int)


async def grant_map(org: str, subject: str) -> GrantMap:
    """The grants `subject`, under any of its spellings, holds on `org`."""
    key: _Key = (org, subject.lower())
    hit = _entries.get(key)
    if (
        hit is not None
        and time.monotonic() - hit[0] < settings.grant_cache_ttl_seconds
        and not _written_since(hit[1])
    ):
        _entries.move_to_end(key)
        return hit[2]
    task = _loading.get(key)
    if task is None:
        # A task of its own, so a cancelled request does not fail the ones waiting with it.
        task = asyncio.ensure_future(_load(key, hit))
        _loading[key] = task
        _running.add(task)
        task.add_done_callback(lambda t: _loaded(key, t))
    return await asyncio.shield(task)


def invalidate(org: str, subjects: Iterable[str] | None = None) -> None:
    """Drop the cached grants of `org` whose read covered one of `subjects`
    (every one with None), and any read of the org in flight."""
    _generation[org] += 1
    lowered = None if subjects is None else {s.lower() for s in subjects}
    for key, entry in list(_entries.items()):
        if key[0] == org and (lowered is None or lowered & (entry[2].spellings | {key[1]})):
            del _entries[key]
    for key in [k for k in _loading if k[0] == org]:
        del _loading[key]
    _mark_written()


def _marker() -> str | None:
    """The file this server's web processes mark a grant write in, when there
    is more than one of them (`LOOM_WEB_WORKERS`): the others read its time."""
    if settings.web_workers <= 1:
        return None
    from server.core.runtime_files import server_file

    try:
        return server_file("grant-writes")
    except OSError:
        return None


def _written_since(began_ns: int) -> bool:
    """Whether another web process of this server wrote a grant since a read
    began at `began_ns`."""
    path = _marker()
    if path is None:
        return False
    try:
        return os.stat(path).st_mtime_ns >= began_ns
    except FileNotFoundError:
        return False
    except OSError:
        return True  # unreadable: read the grants again rather than trust the entry


def _mark_written() -> None:
    path = _marker()
    if path is None:
        return
    from server.core.runtime_files import open_private

    try:
        fd = open_private(path, os.O_WRONLY | os.O_CREAT)
        try:
            os.utime(fd)
        finally:
            os.close(fd)
    except OSError as e:
        log.warning("grant cache: could not mark a grant write in %s: %s", path, e)


def clear() -> None:
    _entries.clear()
    _loading.clear()


async def _load(key: _Key, hit: _Entry | None) -> GrantMap:
    org = key[0]
    generation = _generation[org]
    began = time.time_ns()
    try:
        fresh = await _read(org, key[1])
    except Exception as e:
        if (
            hit is not None
            and time.monotonic() - hit[0] < settings.jwt_revocation_max_stale_seconds
        ):
            return hit[2]
        raise BackendUnavailableError(
            "grants could not be read; retry shortly", operation="grant_map"
        ) from e
    if _generation[org] == generation:
        _entries[key] = (time.monotonic(), began, fresh)
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
    it (`server.auth.aliases`), folded per namespace. Only the spelling itself
    and its active account's pair (`held`) set the rank there. Its deactivated
    account's pair, and the spellings a release kept (`released_aliases`, never
    the row's current owner), still tie it at rank 0 (`capping`): their rows
    may be the revocations that cap, never a grant that admits. One row: the
    namespaces' ranks, and every spelling read, for `invalidate`."""
    from server.core.ch import get_client, own_write_reads

    human = bool(subject) and subject != "*" and not subject.startswith("sa:")
    with own_write_reads():
        row = await get_client().query_one(
            """
            WITH (
                SELECT (
                    arrayFlatten(groupArrayIf(pair, own AND live)),
                    arrayFlatten(groupArray(
                        arrayConcat(if(own AND NOT live, pair, []), if(own, [], released_aliases))
                    ))
                )
                FROM (
                    -- `own`: the row is the subject's account; else it matched on
                    -- `released_aliases` alone, and its current owner is someone else.
                    SELECT [lower(owner_email), lower(contact_email)] AS pair,
                           released_aliases,
                           contact_email != '' AND has(pair, {sub:String}) AS own,
                           active AS live
                    FROM loom.namespaces FINAL
                    WHERE {human:Bool} AND org = {org:String}
                      AND ((contact_email != ''
                            AND has([lower(owner_email), lower(contact_email)], {sub:String}))
                           OR has(released_aliases, {sub:String}))
                )
            ) AS ties,
            arrayPushFront(ties.1, {sub:String}) AS held,
            ties.2 AS capping
            SELECT groupArray((namespace, rank)) AS grants,
                   arrayConcat(held, capping) AS spellings
            FROM (
                SELECT namespace,
                       max(if({live} AND has(held, lower(subject)), {rank}, 0)) AS rank
                FROM loom.namespace_grants FINAL
                WHERE org = {org:String}
                  AND (has(held, lower(subject)) OR has(capping, lower(subject)))
                GROUP BY namespace
            )
            """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
            params={"org": org, "sub": subject, "human": human},
        )
    row = row or {}
    return GrantMap(
        ranks={str(ns): int(rank) for ns, rank in row.get("grants") or []},
        spellings=frozenset(str(s) for s in row.get("spellings") or [subject]),
    )
