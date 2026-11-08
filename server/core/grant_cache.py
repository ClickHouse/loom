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
the org's watermark, which each process polls. The watermark counts each kind
of change apart (grants, groups, the collection tree), so a grant change does
not reload an org's tree or groups. An entry read before its
org's watermark moved is not served, and none is while the last poll is older
than `acl_watermark_max_stale_seconds`; the TTL bounds a change whose watermark
row was never written. A failed re-read serves the old entry while it is
younger than `jwt_revocation_max_stale_seconds`, the bound a token check
already lives with, and refuses (503) past it; an entry a watermark move has
since superseded is never served so, whoever that write was for.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import time
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.core import readiness
from server.core.errors import BackendUnavailableError
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT, VERIFIED_EMAIL_SQL
from server.core.principals import has_aliases
from server.settings import settings

log = logging.getLogger(__name__)

_MAX_ENTRIES = 10_000

# The kinds of change the watermark counts apart: what each one drops.
GRANTS, GROUPS, COLLECTIONS = "grants", "groups", "collections"


@dataclass(frozen=True, slots=True)
class GrantMap:
    """One subject's grants on one org: every namespace it has a row on, of
    any status, and the highest live grant rank there (0 when none is live);
    the spellings the read covered; and of those, the ones the subject holds:
    itself first, then the handle and email of each active account of it whose
    email is verified."""

    ranks: dict[str, int]
    spellings: frozenset[str] = frozenset()
    held: tuple[str, ...] = ()

    def rank(self, namespace: str) -> int | None:
        return self.ranks.get(namespace)


_Key = tuple[str, str]
# (when stored, monotonic; the mark when its read began; the value)
_Entry = tuple[float, int | None, Any]
# Each subject's grants, keyed (org, lowered subject).
_entries: OrderedDict[_Key, _Entry] = OrderedDict()
# The org values the watermark tracks (`org_value`): the collection tree
# (COLLECTIONS) and the group members (GROUPS), keyed (org, kind) as the marks are.
_org_values: OrderedDict[_Key, _Entry] = OrderedDict()
# The read in flight per key of each, and the mark when it began.
_Loading = dict[_Key, tuple[asyncio.Task[Any], int | None]]
_loading: _Loading = {}
_org_loading: _Loading = {}
# Every read until it ends: an invalidation drops it from its `_loading`, not from here.
_running: set[asyncio.Task[Any]] = set()
# Bumped by every invalidation of (org, kind), so a read that began before it
# is not stored.
_generation: defaultdict[_Key, int] = defaultdict(int)


async def grant_map(org: str, subject: str) -> GrantMap:
    """The grants `subject`, under any of its spellings, holds on `org`."""
    key: _Key = (org, subject.lower())
    writes = _writes(org, GRANTS)
    hit = _entries.get(key)
    if hit is not None and _served(hit, writes):
        _entries.move_to_end(key)
        served: GrantMap = hit[2]
        return served
    # Only an entry the TTL aged may stand in for a failed read, not one a
    # watermark move superseded; while the watermark is stale, one read at the
    # last mark seen still may (no move since was seen).
    current = hit if hit is not None and _may_stand_in(hit, (org, GRANTS), writes) else None
    loaded: GrantMap = await _shared_load(
        _loading, key, writes, lambda: _load(key, current, writes)
    )
    return loaded


async def org_value[T](kind: str, org: str, load: Callable[[str], Awaitable[T]]) -> T:
    """`load(org)`, kept as a grant map is: until this process makes a change
    of `kind` in the org, the org's watermark of `kind` moves, or the TTL
    passes."""
    key = (org, kind)
    writes = _writes(org, kind)
    hit = _org_values.get(key)
    if hit is not None and _served(hit, writes):
        _org_values.move_to_end(key)
        served: T = hit[2]
        return served
    loaded: T = await _shared_load(
        _org_loading, key, writes, lambda: _load_org_value(key, load, writes)
    )
    return loaded


def _served(hit: _Entry, writes: int | None) -> bool:
    """Whether a cached entry answers: read at the current mark, within the TTL."""
    return hit[1] == writes and time.monotonic() - hit[0] < settings.grant_cache_ttl_seconds


async def _shared_load(
    loading: _Loading, key: _Key, writes: int | None, start: Callable[[], Awaitable[Any]]
) -> Any:
    """One read per key at a time. A request joins the read in flight when it
    began at the mark the request reads at; a stale watermark (a negative mark)
    serves nothing cached but still joins a read begun on one, never one begun
    on a mark a watermark move has since moved. Else it starts its own: a task
    of its own, so a cancelled request does not fail the others."""
    pending = loading.get(key)
    if pending is not None and (pending[1] == writes or (_unread(writes) and _unread(pending[1]))):
        task = pending[0]
    else:
        task = asyncio.ensure_future(start())
        loading[key] = (task, writes)
        _running.add(task)
        task.add_done_callback(functools.partial(_loaded, loading, key))
    return await asyncio.shield(task)


def _loaded(loading: _Loading, key: _Key, task: asyncio.Task[Any]) -> None:
    _running.discard(task)
    if loading.get(key, (None, None))[0] is task:
        del loading[key]
    if not task.cancelled():
        task.exception()  # read here, so a failure nobody awaited is not logged as lost


def _keep(entries: OrderedDict[_Key, _Entry], key: _Key, writes: int | None, value: Any) -> None:
    entries[key] = (time.monotonic(), writes, value)
    entries.move_to_end(key)
    while len(entries) > _MAX_ENTRIES:
        entries.popitem(last=False)


def _may_stand_in(hit: _Entry, mark: _Key, writes: int | None) -> bool:
    """Whether a cached entry may answer for a failed re-read: it was read at
    the current `mark`, or, while the watermark is stale, at the last one seen
    (no move since was seen)."""
    if hit[1] == writes:
        return True
    return _watching and _unread(writes) and hit[1] == _marks.get(mark, 0)


async def _load_org_value(
    key: _Key, load: Callable[[str], Awaitable[Any]], writes: int | None
) -> Any:
    generation = _generation[key]
    value = await load(key[0])
    if _generation[key] == generation and not _unread(writes):
        _keep(_org_values, key, writes, value)
    return value


def invalidate(org: str, subjects: Iterable[str] | None = None, *, kind: str = GRANTS) -> None:
    """Drop, in this process, what a change of `kind` in `org` makes stale, and
    any read of it in flight: for GRANTS the cached grants whose read covered
    one of `subjects` (every one with None), else the org's value of `kind`.
    `changed` also tells every other process."""
    _generation[(org, kind)] += 1
    if kind != GRANTS:
        _org_values.pop((org, kind), None)
        _org_loading.pop((org, kind), None)
        return
    lowered = None if subjects is None else {s.lower() for s in subjects}
    for key, entry in list(_entries.items()):
        if key[0] == org and (lowered is None or lowered & (entry[2].spellings | {key[1]})):
            del _entries[key]
    for key in [k for k in _loading if k[0] == org]:
        del _loading[key]


@asynccontextmanager
async def announced(
    org: str, subjects: Iterable[str] | None = None, *, kind: str = GRANTS
) -> AsyncIterator[None]:
    """Run a change's writes, then `changed`, even when one raises: a write
    whose reply was lost may have landed, and the retry finds nothing to do."""
    try:
        yield
    finally:
        await changed(org, subjects, kind=kind)


async def changed(org: str, subjects: Iterable[str] | None = None, *, kind: str = GRANTS) -> None:
    """A change of `kind` in `org` was written (a grant, a group, the tree):
    drop it here at once, and move the org's watermark of `kind` so every other
    process drops it at its next poll. A watermark row that is never written
    leaves the change to the TTL."""
    invalidate(org, subjects, kind=kind)
    from server.core.ch import get_client

    try:
        await get_client().insert(
            "loom.acl_versions",
            [watermark_row(org, kind)],
            # Two changes in one millisecond write equal rows; each must count.
            settings={"async_insert": 0, "wait_for_async_insert": 1, "insert_deduplicate": 0},
        )
    except Exception as e:  # noqa: BLE001 — the grant write landed; the TTL bounds it elsewhere
        log.warning("grant cache: watermark for org %s not written: %s", org, e)
    from server.core import pg

    # Who may read which peer changed: a write admitted before it starts over.
    await pg.bump_policy(org)


def watermark_row(org: str, kind: str = GRANTS) -> dict[str, Any]:
    """One whole loom.acl_versions row: one more change of `kind` in `org`."""
    return {
        "org": org,
        "kind": kind,
        "changes": 1,
        "updated_at": datetime.now(UTC).replace(tzinfo=None),
    }


def clear() -> None:
    global _watching, _polled_at
    _entries.clear()
    _loading.clear()
    _org_values.clear()
    _org_loading.clear()
    _watching = False
    _polled_at = None
    _seen.clear()
    _marks.clear()


# Every process polls the change counts in `loom.acl_versions` and keeps a mark
# per (org, kind): a count that moves when that change count moved. An entry, or
# a read in flight, is served only while its mark is the one its read began at.
# A process that never polls (the CLI, a unit test) keeps no marks and lives on
# the TTL.
_watching = False
_polled_at: float | None = None  # monotonic, the last poll that succeeded
_seen: dict[tuple[str, str], int] = {}  # (org, kind) -> its change count, last read
_marks: dict[tuple[str, str], int] = {}
_unreadable = itertools.count(-1, -1)
_warned: set[str] = set()


def _unread(writes: int | None) -> bool:
    return writes is not None and writes < 0


def _writes(org: str, kind: str) -> int | None:
    """The mark of (org, kind) in this process; None when it does not poll. A watermark
    older than `acl_watermark_max_stale_seconds` is a fresh negative mark each
    time, so no entry is served on it (a read in flight on one is still shared)."""
    if not _watching:
        return None
    if not fresh():
        return next(_unreadable)
    return _marks.get((org, kind), 0)


def fresh() -> bool:
    """Whether this process's last good poll is within the stale bound."""
    return (
        _polled_at is not None
        and time.monotonic() - _polled_at <= settings.acl_watermark_max_stale_seconds
    )


async def poll() -> None:
    """Read every change count once; move the mark of each (org, kind) whose
    count moved. A failure keeps the marks; `_writes` stops trusting them past the
    stale bound."""
    global _watching, _polled_at
    from server.core.ch import get_client, sequential_reads

    _watching = True
    try:
        # Sequential: a lagging replica would hide a change, and reads that
        # alternate between replicas would see counts move back and forth.
        with sequential_reads():
            rows = await get_client().query_all(
                "SELECT org, kind, sum(changes) AS n FROM loom.acl_versions GROUP BY org, kind"
            )
        for r in rows:
            mark, count = (str(r["org"]), str(r["kind"])), int(r["n"])
            if _seen.get(mark) != count:
                _seen[mark] = count
                _marks[mark] = _marks.get(mark, 0) + 1
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
    generation = _generation[(org, GRANTS)]
    try:
        fresh = await _read(org, key[1])
    except Exception as e:
        if (
            hit is not None
            and time.monotonic() - hit[0] < settings.jwt_revocation_max_stale_seconds
        ):
            stale: GrantMap = hit[2]
            return stale
        raise BackendUnavailableError(
            "grants could not be read; retry shortly", operation="grant_map"
        ) from e
    # A read on a stale watermark is not kept: it would replace the entry read
    # at the last mark seen, the one a failed re-read may still fall back to.
    if _generation[(org, GRANTS)] == generation and not _unread(writes):
        _keep(_entries, key, writes, fresh)
    return fresh


async def _read(org: str, subject: str) -> GrantMap:
    """One read: the subject's rows under every spelling the directory ties to
    it (`server.auth.aliases`), folded per namespace. Only the spelling itself
    and its active account's pair, when the account's email is verified
    (`held`), set the rank there. An unverified pair (a self-signup's email,
    which anyone could have typed), its deactivated account's pair, and the
    spellings a release kept (`released_aliases`, never the row's current
    owner), still tie it at rank 0 (`capping`): their rows may be the
    revocations that cap, never a grant that admits. One row: the
    namespaces' ranks, and every spelling read, for `invalidate`."""
    from server.core.ch import get_client, own_write_reads

    human = has_aliases(subject)
    with own_write_reads():
        row = await get_client().query_one(
            """
            WITH (
                SELECT (
                    arrayFlatten(groupArrayIf(pair, own AND live AND verified)),
                    arrayFlatten(groupArray(arrayConcat(
                        if(own AND NOT (live AND verified), pair, []),
                        if(own, [], released_aliases)
                    )))
                )
                FROM (
                    -- `own`: the row is the subject's account; else it matched on
                    -- `released_aliases` alone, and its current owner is someone else.
                    SELECT [lower(owner_email), lower(contact_email)] AS pair,
                           released_aliases,
                           contact_email != '' AND has(pair, {sub:String}) AS own,
                           active AS live,
                           {verified} AS verified
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
                   arrayConcat(held, capping) AS spellings,
                   held
            FROM (
                SELECT namespace,
                       max(if({live} AND has(held, lower(subject)), {rank}, 0)) AS rank
                FROM loom.namespace_grants FINAL
                WHERE org = {org:String}
                  AND (has(held, lower(subject)) OR has(capping, lower(subject)))
                GROUP BY namespace
            )
            """.replace("{live}", LIVE_GRANT)
            .replace("{rank}", GRANT_RANK_SQL)
            .replace("{verified}", VERIFIED_EMAIL_SQL),
            params={"org": org, "sub": subject, "human": human},
        )
    row = row or {}
    return GrantMap(
        ranks={str(ns): int(rank) for ns, rank in row.get("grants") or []},
        spellings=frozenset(str(s) for s in row.get("spellings") or [subject]),
        held=tuple(str(s) for s in row.get("held") or [subject]),
    )
