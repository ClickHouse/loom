"""Each subject's grant rows on an org, cached per process for the request path.

A token's claim on a namespace stands only where its subject holds no grant
row there; where it holds one, the claim is capped at that grant
(`authz.claim_cap`). That needs the subject's rows on every request, so one
read per (org, subject), every spelling of the person included, is kept for
`grant_cache_ttl_seconds`.

A grant write in this process drops the entries whose read covered the
spelling it wrote, at once; a write to the accounts that tie a person's
spellings drops its org's. This server's other web processes read again at
once too: the write counts itself in a file they share, and an entry read before
that count moved is not served (one count per org, so across processes a write
drops the org's entries). Another server sees either within the TTL. A failed
re-read serves the old entry while it is younger than
`jwt_revocation_max_stale_seconds`, the bound a token check already lives with,
and refuses (503) past it.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
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
# (when stored, monotonic; the org's write count when its read began; the grants)
_Entry = tuple[float, int | None, GrantMap]
_entries: OrderedDict[_Key, _Entry] = OrderedDict()
# The read in flight per key, and the org's write count when it began.
_loading: dict[_Key, tuple[asyncio.Task[GrantMap], int | None]] = {}
# Every read until it ends: an invalidation drops it from `_loading`, not from here.
_running: set[asyncio.Task[GrantMap]] = set()
# Bumped by every invalidation, so a read that began before it is not stored.
_generation: defaultdict[str, int] = defaultdict(int)


async def grant_map(org: str, subject: str) -> GrantMap:
    """The grants `subject`, under any of its spellings, holds on `org`."""
    key: _Key = (org, subject.lower())
    writes = _writes(org)
    hit = _entries.get(key)
    if (
        hit is not None
        and time.monotonic() - hit[0] < settings.grant_cache_ttl_seconds
        and hit[1] == writes
    ):
        _entries.move_to_end(key)
        return hit[2]
    loading = _loading.get(key)
    # An unreadable count (negative) serves nothing cached, but still joins a read.
    if loading is not None and (loading[1] == writes or (writes is not None and writes < 0)):
        task = loading[0]
    else:
        # Only an entry the TTL aged may stand in for a failed read, not one a write
        # superseded. A task of its own, so a cancelled request does not fail the others.
        current = hit if hit is not None and hit[1] == writes else None
        task = asyncio.ensure_future(_load(key, current, writes))
        _loading[key] = (task, writes)
        _running.add(task)
        task.add_done_callback(lambda t: _loaded(key, t))
    return await asyncio.shield(task)


def invalidate(org: str, subjects: Iterable[str] | None = None) -> None:
    """Drop the cached grants of `org` whose read covered one of `subjects`
    (every one with None), and any read of the org in flight; and count the
    write for this server's other web processes."""
    _generation[org] += 1
    lowered = None if subjects is None else {s.lower() for s in subjects}
    for key, entry in list(_entries.items()):
        if key[0] == org and (lowered is None or lowered & (entry[2].spellings | {key[1]})):
            del _entries[key]
    for key in [k for k in _loading if k[0] == org]:
        del _loading[key]
    before, after = _count_write(org)
    if before is not None and after == before + 1:
        # Only this write since: the entries it left are still current.
        for key, (at, seen, grants) in list(_entries.items()):
            if key[0] == org and seen == before:
                _entries[key] = (at, after, grants)


def clear() -> None:
    global _marker_base
    _entries.clear()
    _loading.clear()
    _marker_base = _UNRESOLVED
    _marker_paths.clear()


# This server's web processes (`LOOM_WEB_WORKERS` > 1) count each org's grant
# writes in a file of its own they share: one byte per write, its size the count.
# An entry, or a read in flight, is served only while the count is the one its
# read began at. A single process needs none.
_UNRESOLVED = "?"
_marker_base: str | None = _UNRESOLVED
_marker_paths: dict[str, str] = {}
_unreadable = itertools.count(-1, -1)
_warned: set[str] = set()


def _marker(org: str) -> str | None:
    global _marker_base
    if settings.web_workers <= 1:
        return None
    if _marker_base == _UNRESOLVED:
        from server.core.runtime_files import server_file

        try:
            _marker_base = server_file("grant-writes")
        except OSError as e:
            _marker_base = None
            _warn_once("dir", "no runtime directory; grant writes reach siblings by TTL: %s", e)
    if _marker_base is None:
        return None
    path = _marker_paths.get(org)
    if path is None:
        path = _marker_paths[org] = (
            f"{_marker_base}.{hashlib.sha256(org.encode()).hexdigest()[:16]}"
        )
    return path


def _writes(org: str) -> int | None:
    """The org's grant write count on this server; None with one web process.
    An unreadable count is a fresh one each time, so nothing is served on it."""
    path = _marker(org)
    if path is None:
        return None
    try:
        return os.stat(path).st_size
    except FileNotFoundError:
        return 0
    except OSError as e:
        _warn_once("read", "cannot read %s; grants are read again on every request: %s", path, e)
        return next(_unreadable)


def _count_write(org: str) -> tuple[int | None, int | None]:
    """Add this write to the org's count: (count before, count after)."""
    path = _marker(org)
    if path is None:
        return None, None
    from server.core.runtime_files import open_private

    try:
        fd = open_private(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            before = os.fstat(fd).st_size
            os.write(fd, b".")
            return before, os.fstat(fd).st_size
        finally:
            os.close(fd)
    except OSError as e:
        _warn_once("write", "cannot count a grant write in %s: %s", path, e)
        return None, None


def _warn_once(what: str, message: str, *args: object) -> None:
    if what not in _warned:
        _warned.add(what)
        log.warning("grant cache: " + message, *args)


async def _load(key: _Key, hit: _Entry | None, writes: int | None) -> GrantMap:
    org = key[0]
    generation = _generation[org]
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
        _entries[key] = (time.monotonic(), writes, fresh)
        _entries.move_to_end(key)
        while len(_entries) > _MAX_ENTRIES:
            _entries.popitem(last=False)
    return fresh


def _loaded(key: _Key, task: asyncio.Task[GrantMap]) -> None:
    _running.discard(task)
    loading = _loading.get(key)
    if loading is not None and loading[0] is task:
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
