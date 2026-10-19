"""Each subject's grant rows on an org, cached per process for the request path.

A token's claim on a namespace stands only where its subject holds no grant
row there; where it holds one, the claim is capped at that grant
(`authz.claim_cap`). That needs the subject's rows on every request, so one
read per (org, subject), every spelling of the person included, is kept for
`grant_cache_ttl_seconds`.

A grant write in this process drops the entries whose read covered the
spelling it wrote, at once; a write to the accounts that tie a person's
spellings drops its org's. Every other process, on this server or another,
drops the org's entries within `acl_watermark_poll_seconds`: the write moves
the org's watermark row, which each process polls. An entry read before its
org's watermark moved is not served, and none is while the last poll is older
than `acl_watermark_max_stale_seconds`; the TTL bounds a change whose watermark
row was never written. A failed re-read serves the old entry while it is
younger than `jwt_revocation_max_stale_seconds`, the bound a token check
already lives with, and refuses (503) past it; an entry a watermark move has
since superseded is never served so, whoever that write was for.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
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
    # An unreadable count (negative) serves nothing cached, but still joins a read
    # begun on one: never one begun on a count a sibling's write has since moved.
    if loading is not None and (loading[1] == writes or (_unread(writes) and _unread(loading[1]))):
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
    (every one with None), and any read of the org in flight; and move the
    org's watermark for every other process."""
    _generation[org] += 1
    lowered = None if subjects is None else {s.lower() for s in subjects}
    for key, entry in list(_entries.items()):
        if key[0] == org and (lowered is None or lowered & (entry[2].spellings | {key[1]})):
            del _entries[key]
    for key in [k for k in _loading if k[0] == org]:
        del _loading[key]
    before, after = _count_write(org)
    if before is not None:
        # The entries this write left are still current.
        for key, (at, seen, grants) in list(_entries.items()):
            if key[0] == org and seen == before:
                _entries[key] = (at, after, grants)
    _announce(org)


def clear() -> None:
    global _watching, _polled_at
    _entries.clear()
    _loading.clear()
    _watching = False
    _polled_at = None
    _seen.clear()
    _marks.clear()


# Every process polls each org's watermark row in `loom.acl_versions` and keeps
# a mark per org: a count that moves when the watermark it read moved, or when
# this process changed the org's grants. An entry, or a read in flight, is
# served only while its org's mark is the one its read began at. A process
# that never polls (the CLI, a unit test) keeps no marks and lives on the TTL.
_watching = False
_polled_at: float | None = None  # monotonic, the last poll that succeeded
_seen: dict[str, tuple[int, int]] = {}  # org -> (rows, sum of versions) last read
_marks: defaultdict[str, int] = defaultdict(int)
_unreadable = itertools.count(-1, -1)
_announcing: set[asyncio.Task[None]] = set()


def _unread(writes: int | None) -> bool:
    return writes is not None and writes < 0


def _writes(org: str) -> int | None:
    """The org's mark in this process; None when it does not poll. A watermark
    older than `acl_watermark_max_stale_seconds` is a fresh negative mark each
    time, so no entry is served on it (a read in flight on one is still shared)."""
    if not _watching:
        return None
    if (
        _polled_at is None
        or time.monotonic() - _polled_at > settings.acl_watermark_max_stale_seconds
    ):
        return next(_unreadable)
    return _marks[org]


def _count_write(org: str) -> tuple[int | None, int | None]:
    """Move the org's mark for this write: (mark before, mark after)."""
    if not _watching:
        return None, None
    before = _marks[org]
    _marks[org] = before + 1
    return before, before + 1


async def poll() -> None:
    """Read every org's watermark once; move the mark of each org whose
    watermark moved. A failure keeps the marks, and `_writes` stops trusting
    them once the last success is older than the stale bound."""
    global _watching, _polled_at
    from server.core.ch import get_client, sequential_reads

    _watching = True
    try:
        # Sequential: a replica that has not yet seen the watermark row would
        # also miss the grant row written before it.
        with sequential_reads():
            rows = await get_client().query_all(
                "SELECT org, count() AS n, sum(version) AS s FROM loom.acl_versions GROUP BY org"
            )
    except Exception as e:  # noqa: BLE001 — a failed poll keeps the marks; staleness refuses the cache
        _warn_once("poll", "cannot read the grant watermark; cached grants expire: %s", e)
        return
    _warned.discard("poll")
    for r in rows:
        org, pair = str(r["org"]), (int(r["n"]), int(r["s"]))
        if _seen.get(org) != pair:
            _seen[org] = pair
            _marks[org] += 1
    _polled_at = time.monotonic()


def start_poll_loop(interval_s: float, *, name: str = "loom-grant-watermark") -> asyncio.Task[None]:
    """Poll the watermark every `interval_s` seconds until cancelled."""

    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("grant_cache.poll")
        while True:
            await asyncio.sleep(interval_s)
            await poll()

    return asyncio.get_running_loop().create_task(_run(), name=name)


def _announce(org: str) -> None:
    """Move the org's watermark so every other process drops its entries. It
    runs after the grant write, off the request; if it never lands, the TTL
    bounds the change on other processes."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_insert_version(org))
    _announcing.add(task)
    task.add_done_callback(_announcing.discard)


async def _insert_version(org: str) -> None:
    from server.core.ch import get_client

    try:
        await get_client().insert(
            "loom.acl_versions",
            [{"org": org, "version": time.time_ns()}],
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
    except Exception as e:  # noqa: BLE001 — the grant write landed; the TTL bounds it elsewhere
        log.warning("grant cache: watermark for org %s not written: %s", org, e)


_warned: set[str] = set()


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
