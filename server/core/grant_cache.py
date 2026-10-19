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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.core import readiness
from server.core.errors import BackendUnavailableError
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT
from server.core.principals import ONE_SPELLING
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
# (when stored, monotonic; the org's mark when its read began; the grants)
_Entry = tuple[float, int | None, GrantMap]
_entries: OrderedDict[_Key, _Entry] = OrderedDict()
# The read in flight per key, and the org's mark when it began.
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
    # A stale watermark (a negative mark) serves nothing cached, but still joins a
    # read begun on one: never one begun on a mark a watermark move has since moved.
    if loading is not None and (loading[1] == writes or (_unread(writes) and _unread(loading[1]))):
        task = loading[0]
    else:
        # Only an entry the TTL aged may stand in for a failed read, not one a
        # watermark move superseded; while the watermark is stale, one read at the
        # last mark seen still may (no move since was seen). A task of its own, so a
        # cancelled request does not fail the others.
        current = hit if hit is not None and _may_stand_in(hit, org, writes) else None
        task = asyncio.ensure_future(_load(key, current, writes))
        _loading[key] = (task, writes)
        _running.add(task)
        task.add_done_callback(lambda t: _loaded(key, t))
    return await asyncio.shield(task)


def _may_stand_in(hit: _Entry, org: str, writes: int | None) -> bool:
    """Whether a cached entry may answer for a failed re-read: it was read at
    the org's current mark, or, while the watermark is stale, at the last mark
    seen (no move since was seen)."""
    if hit[1] == writes:
        return True
    return _watching and _unread(writes) and hit[1] == _marks.get(org, 0)


# Per-org values that change with the org's grants (`org_value`): its
# collection tree and its group members. (kind, org) -> (when stored, mark, value).
_org_values: OrderedDict[tuple[str, str], tuple[float, int | None, Any]] = OrderedDict()


async def org_value[T](kind: str, org: str, load: Callable[[str], Awaitable[T]]) -> T:
    """`load(org)`, kept as a grant map is: until this process changes the
    org's grants, its watermark moves, or the TTL passes."""
    key = (kind, org)
    writes = _writes(org)
    hit = _org_values.get(key)
    if (
        hit is not None
        and hit[1] == writes
        and time.monotonic() - hit[0] < settings.grant_cache_ttl_seconds
    ):
        _org_values.move_to_end(key)
        return hit[2]  # type: ignore[no-any-return]
    generation = _generation[org]
    value = await load(org)
    if _generation[org] == generation and not _unread(writes):
        _org_values[key] = (time.monotonic(), writes, value)
        _org_values.move_to_end(key)
        while len(_org_values) > _MAX_ENTRIES:
            _org_values.popitem(last=False)
    return value


def invalidate(org: str, subjects: Iterable[str] | None = None) -> None:
    """Drop, in this process, the cached grants of `org` whose read covered one
    of `subjects` (every one with None), its per-org values, and any read of the
    org in flight. `changed` also tells every other process."""
    _generation[org] += 1
    for key in [k for k in _org_values if k[1] == org]:
        del _org_values[key]
    lowered = None if subjects is None else {s.lower() for s in subjects}
    for key, entry in list(_entries.items()):
        if key[0] == org and (lowered is None or lowered & (entry[2].spellings | {key[1]})):
            del _entries[key]
    for key in [k for k in _loading if k[0] == org]:
        del _loading[key]


async def changed(org: str, subjects: Iterable[str] | None = None) -> None:
    """A grant change in `org` was written: drop it here at once, and move the
    org's watermark so every other process drops it at its next poll. A
    watermark row that is never written leaves the change to the TTL."""
    invalidate(org, subjects)
    from server.core.ch import get_client

    try:
        await get_client().insert(
            "loom.acl_versions",
            [watermark_row(org)],
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
    except Exception as e:  # noqa: BLE001 — the grant write landed; the TTL bounds it elsewhere
        log.warning("grant cache: watermark for org %s not written: %s", org, e)


def watermark_row(org: str) -> dict[str, Any]:
    """One whole loom.acl_versions row: a fresh version for `org`."""
    return {
        "org": org,
        "version": time.time_ns(),
        "updated_at": datetime.now(UTC).replace(tzinfo=None),
    }


def clear() -> None:
    global _watching, _polled_at, _total
    _entries.clear()
    _loading.clear()
    _org_values.clear()
    _watching = False
    _polled_at = None
    _total = None
    _seen.clear()
    _marks.clear()


# Every process polls the watermark rows in `loom.acl_versions` and keeps a mark
# per org: a count that moves when the org's watermark moved. An entry, or a read
# in flight, is served only while its org's mark is the one its read began at. A
# process that never polls (the CLI, a unit test) keeps no marks and lives on the TTL.
_watching = False
_polled_at: float | None = None  # monotonic, the last poll that succeeded
_total: tuple[int, int] | None = None  # (rows, sum of versions) over every org, last read
_seen: dict[str, tuple[int, int]] = {}  # org -> (rows, sum of versions) last read
_marks: dict[str, int] = {}
_unreadable = itertools.count(-1, -1)
_warned: set[str] = set()


def _unread(writes: int | None) -> bool:
    return writes is not None and writes < 0


def _writes(org: str) -> int | None:
    """The org's mark in this process; None when it does not poll. A watermark
    older than `acl_watermark_max_stale_seconds` is a fresh negative mark each
    time, so no entry is served on it (a read in flight on one is still shared)."""
    if not _watching:
        return None
    if not fresh():
        return next(_unreadable)
    return _marks.get(org, 0)


def fresh() -> bool:
    """Whether this process's last good poll is within the stale bound."""
    return (
        _polled_at is not None
        and time.monotonic() - _polled_at <= settings.acl_watermark_max_stale_seconds
    )


async def poll() -> None:
    """Read the watermark once: the total over every org, and each org's pair
    only when the total moved. Moves the mark of each org whose pair moved. A
    failure keeps the marks; `_writes` stops trusting them past the stale bound."""
    global _watching, _polled_at, _total
    from server.core.ch import get_client, sequential_reads

    _watching = True
    try:
        # Sequential, so a process whose reads alternate between a lagging
        # replica and a current one does not see the total move back and forth.
        with sequential_reads():
            row = await get_client().query_one(
                "SELECT count() AS n, sum(version) AS s FROM loom.acl_versions"
            )
        total = (int((row or {}).get("n") or 0), int((row or {}).get("s") or 0))
        if total != _total:
            rows = await get_client().query_all(
                "SELECT org, count() AS n, sum(version) AS s FROM loom.acl_versions GROUP BY org"
            )
            for r in rows:
                org, pair = str(r["org"]), (int(r["n"]), int(r["s"]))
                if _seen.get(org) != pair:
                    _seen[org] = pair
                    _marks[org] = _marks.get(org, 0) + 1
            _total = total
    except Exception as e:  # noqa: BLE001 — a failed poll keeps the marks; staleness refuses the cache
        _warn_once("poll", "cannot read the grant watermark; cached grants expire: %s", e)
        return
    _warned.discard("poll")
    _polled_at = time.monotonic()


def readiness_check() -> readiness.CheckResult:
    """Not required: a stale watermark only costs a grant read per request."""
    return readiness.result(
        fresh(), "the grant watermark is stale; grants are read on every request"
    )


def start_poll_loop(interval_s: float, *, name: str = "loom-grant-watermark") -> asyncio.Task[None]:
    """Poll the watermark every `interval_s` seconds until cancelled."""

    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("grant_cache.poll")
        while True:
            await asyncio.sleep(interval_s)
            await poll()

    return asyncio.get_running_loop().create_task(_run(), name=name)


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

    human = bool(subject) and subject != "*" and not subject.startswith(ONE_SPELLING)
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
