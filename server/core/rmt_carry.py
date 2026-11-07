"""Keep columns this build does not know through a ReplacingMergeTree version write.

During a rolling upgrade N-1 writes version rows without the columns N added,
which would reset them under FINAL. `carry_unknown_columns` fills those columns
from the newest stored version of each key. The protocol is in
docs/architecture/rolling-upgrades.md (rule 2); its invariants here:

- A carried write runs only while this process's acknowledgement of the lists
  it takes (`loom.carry_acks`) is younger than `_ADMIT_S` (`_admit`).
- No newer lists are acknowledged while a write that took older ones is in
  flight or pinned (`writing`, `_pinned`).
- A boot serves only once every live process acknowledges its schema and no
  process without a current, non-empty acknowledgement has a carried insert in
  `system.processes` (`await_acks`).
- ClickHouse refuses a batch that starts after its deadline or meets a column
  outside the lists it was built against (`fence_columns`, `send_deadline`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import math
import os
import socket
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from server.core.ch_names import database, logical, split_table
from server.core.ch_writes import async_insert_settings
from server.core.errors import BackendUnavailableError

log = logging.getLogger(__name__)

# How often the schema is checked: a migration's new column is carried at most
# this long after its ALTER lands.
_POLL_S = 2.0
_LOOKUP_CHUNK = 500
ACK_TABLE = "loom.carry_acks"
# An acknowledgement older than this is a process that is gone.
_ACK_LIVE_S = 30
# A carried write is admitted only while the last published acknowledgement is
# younger than this: a boot still counts the process when the write lands.
_ADMIT_S = _ACK_LIVE_S / 2
# The watcher re-publishes this often, so a write rarely finds it stale.
_ACK_REFRESH_S = _ADMIT_S / 2
# How long a booting process waits for every live process to acknowledge.
ACK_WAIT_S = 120.0
_STORED_SQL = """
    SELECT database, table, name FROM system.columns
    WHERE (database, table) IN {keys:Array(Tuple(String, String))}
      AND default_kind NOT IN ('MATERIALIZED', 'ALIAS', 'EPHEMERAL')
"""


class _Table:
    def __init__(self, key: tuple[str, ...], known: frozenset[str]) -> None:
        self.key = key
        self.known = known
        # The stored columns; None until first read.
        self.stored: frozenset[str] | None = None

    def key_of(self, row: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(row.get(k, "")) for k in self.key)


# The tables whose writes carry, registered by their row builders.
_TABLES: dict[str, _Table] = {}
_WARNED: set[tuple[str, str]] = set()
_watcher: asyncio.Task[None] | None = None
# Each read of the lists that changed them is a generation; writes in flight per
# generation, and what each generation's lists were.
_gen = 0
_inflight: Counter[int] = Counter()
_prints: dict[int, dict[str, str]] = {}
_published: tuple[str, float] | None = None  # (fingerprints, monotonic time)
_read_ok = False


class _Write:
    """One carried insert: per table, the stored lists it took, the query id of
    every send it made, and the latest start deadline and limit of those sends."""

    def __init__(self) -> None:
        self.taken: dict[str, frozenset[str]] = {}
        self.query_ids: list[str] = []
        # Sends the server answered (rows or its own error): their queries ended.
        self.ended: set[str] = set()
        self.deadline = 0.0  # server clock, epoch seconds
        self.max_s = 0.0


@dataclass
class _Pin:
    """A write that ended without the server confirming its end."""

    gen: int
    query_ids: list[str]
    deadline: float  # no query of it starts later (server clock)
    release_at: float  # nor runs later: deadline + max_execution_time + margin
    error: str = ""
    warned: bool = False


_carried: contextvars.ContextVar[_Write | None] = contextvars.ContextVar(
    "rmt_carry_write", default=None
)
# When (monotonic) the lease this task's sends are made under ends (`under_lease`).
_lease_end: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "rmt_carry_lease_end", default=None
)
_pinned: list[_Pin] = []
# How long a send may take to reach the server and start: a request still
# queued past this is refused by its guard.
_TRANSIT_S = 15.0
# Past the server bound: the clock reading's own error and a commit's tail.
_RELEASE_MARGIN_S = 5.0
# (server clock in epoch seconds, time.monotonic()) at the last measure: the
# server's clock advances with the monotonic one, so a host clock step moves nothing.
_server_anchor: tuple[float, float] | None = None
_skew_at = -math.inf
_SKEW_REFRESH_S = 60.0
# The client the watcher runs on; the settle uses it too.
_watch_ch: Any = None
# A server without the cluster answers for itself.
_CLUSTER_DOESNT_EXIST = 701


def process_id() -> str:
    return f"{socket.gethostname()[:64]}:{os.getpid()}"


# A carried send's query id names its process, so a boot can see the inserts a
# process that died (no ack, no pin) left running on the server.
CARRY_QUERY_PREFIX = "loom-carry/"


def carry_query_id(base: str) -> str:
    return f"{CARRY_QUERY_PREFIX}{process_id()}/{base}"


def _boot_wait_s() -> float:
    """How long a boot waits: never less than `ACK_WAIT_S`, and past the longest
    a dead writer's last insert may run (`_writer_bound_s`)."""
    return max(ACK_WAIT_S, _writer_bound_s() + 10)


def _writer_bound_s() -> float:
    """How long after its last acknowledgement a process can still have a carried
    insert on the server: one last send's start deadline and its limit."""
    return _ACK_LIVE_S + send_bound_s()


def send_bound_s() -> float:
    """How long a send can still run on the server once its start deadline is drawn:
    the transit allowance, its write limit and the clock reading's error."""
    from server.core.ch import write_limit_s

    return _TRANSIT_S + write_limit_s() + _RELEASE_MARGIN_S


def fingerprint(columns: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(columns)).encode()).hexdigest()[:16]


def register(table: str, *, key: Iterable[str], columns: Iterable[str]) -> None:
    """`table` is a ReplacingMergeTree(version) ordered by `key` whose
    builder writes `columns`."""
    _TABLES[table] = _Table(tuple(key), frozenset(columns))


def carries(table: str) -> bool:
    return table in _TABLES


async def _stored(ch: Any, tables: Iterable[str]) -> dict[str, set[str]]:
    tables = list(tables)
    rows = await ch.query_all(_STORED_SQL, {"keys": [split_table(t) for t in tables]})
    stored: dict[str, set[str]] = {table: set() for table in tables}
    for r in rows:
        stored.setdefault(f"{logical(r['database'])}.{r['table']}", set()).add(str(r["name"]))
    return stored


async def _read_columns(ch: Any) -> bool:
    """Read every registered table's stored columns; False when unreadable. A
    table read once keeps its last list; one never read stays unknown."""
    global _gen, _read_ok
    try:
        stored = await _stored(ch, _TABLES)
    except Exception as e:  # noqa: BLE001 — the watcher retries; writes wait for a list
        log.warning("rmt carry: column lists unreadable (%s)", e)
        _read_ok = False
        return False
    for table, t in _TABLES.items():
        t.stored = frozenset(stored[table])
    prints = {table: fingerprint(cols) for table, cols in stored.items()}
    if prints != _prints.get(_gen):
        _gen += 1
        _prints[_gen] = prints
    _read_ok = True
    return True


# A table whose lists this process has not read yet: a boot waits for it.
_UNREAD = "?"


def acknowledged() -> dict[str, str]:
    """Per carried table, the fingerprint of the newest lists no write in flight
    is older than: `_UNREAD` while a write that has not read them yet runs, and
    nothing for a process that never wrote (its first write reads fresh lists)."""
    busy = [g for g, n in _inflight.items() if n > 0]
    if not busy and not _prints:
        return {}
    return _prints.get(min(busy, default=_gen)) or dict.fromkeys(_TABLES, _UNREAD)


async def _publish(ch: Any, *, force: bool = False) -> bool:
    """This process's acknowledgement, when it changed or is due again; False
    when the write failed (the writes then stop once the last one ages)."""
    global _published
    value = json.dumps(acknowledged(), sort_keys=True)
    now = time.monotonic()  # taken before the send: freshness never overstated
    if (
        not force
        and _published
        and _published[0] == value
        and (now - _published[1] < _ACK_REFRESH_S)
    ):
        return True
    try:
        await ch.insert(
            ACK_TABLE,
            [{"process": process_id(), "fingerprints": value}],
            # Durable when it returns; the processes' refreshes share parts.
            settings=async_insert_settings(),
        )
    except Exception as e:  # noqa: BLE001 — re-published next poll; writes wait for it
        log.warning("rmt carry: acknowledgement not written (%s)", e)
        return False
    _published = (value, now)
    return True


def _fresh() -> bool:
    """The last read of the lists succeeded and an acknowledgement sent since is young."""
    return _read_ok and _published is not None and time.monotonic() - _published[1] < _ADMIT_S


_RENEW: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _renewing() -> asyncio.Lock:
    """The lock one renewal at a time holds (per event loop: a lock binds to one)."""
    global _RENEW
    loop = asyncio.get_running_loop()
    if _RENEW is None or _RENEW[0] is not loop:
        _RENEW = (loop, asyncio.Lock())
    return _RENEW[1]


async def _admit(ch: Any, t: _Table) -> None:
    """Admit a carried write to `t`: its lists are ones a fresh, published
    acknowledgement stands for. A stale one (or a table registered since the last
    read) is renewed first, enrolment then lists then acknowledgement;
    BackendUnavailable when any step fails."""
    if _fresh() and t.stored is not None and _server_anchor is not None:
        return
    async with _renewing():  # one renewal for every write that found it stale
        if _fresh() and t.stored is not None and _server_anchor is not None:
            return
        if not (
            await _publish(ch, force=True)
            and await _read_columns(ch)
            and await _publish(ch, force=True)
        ):
            raise BackendUnavailableError(
                "rmt carry: this process's acknowledgement is not current; retry"
            )
        try:
            await _measure_skew(ch)
        except Exception as e:
            raise BackendUnavailableError(f"rmt carry: the server's clock: {e}; retry") from e


def _release(gen: int) -> None:
    _inflight[gen] -= 1
    if not _inflight[gen]:
        del _inflight[gen]


@contextlib.asynccontextmanager
async def writing() -> AsyncIterator[None]:
    """One carried insert, from taking the lists to its last batch: until it
    ends, no newer lists are acknowledged, and its batches are guarded by the
    lists it took (`fence_columns`). One that ends in an error after a send
    stays in flight until the server confirms its queries ended (`_settle`)."""
    gen = _gen
    _inflight[gen] += 1
    write = _Write()
    token = _carried.set(write)
    try:
        yield
    except BaseException:
        unanswered = [q for q in write.query_ids if q not in write.ended]
        if unanswered:
            release_at = write.deadline + write.max_s + _RELEASE_MARGIN_S
            _pinned.append(_Pin(gen, unanswered, write.deadline, release_at))
            _kick_settle()
        else:
            _release(gen)
        raise
    else:
        _release(gen)
    finally:
        _carried.reset(token)


def note_ended(query_id: str) -> None:
    """The server answered this send (its rows, or an error of its own): the query
    is over and holds nothing back."""
    write = _carried.get()
    if write is not None:
        write.ended.add(query_id)


def note_send(query_id: str, deadline: float = 0.0, max_s: float = 0.0) -> None:
    """The client sent one batch of this carried write as `query_id`, to start by
    `deadline` (server clock) and run for at most `max_s`."""
    write = _carried.get()
    if write is not None:
        write.query_ids.append(query_id)
        write.deadline = max(write.deadline, deadline)
        write.max_s = max(write.max_s, max_s)


async def _measure_skew(ch: Any) -> None:
    """The server's clock against this host's, from one round trip's midpoint."""
    global _server_anchor, _skew_at
    sent = time.monotonic()
    row = await ch.query_one("SELECT toUnixTimestamp64Milli(now64(3)) AS ms")
    back = time.monotonic()
    if row is None:
        raise BackendUnavailableError("rmt carry: the server's clock cannot be read; retry")
    _server_anchor = (int(row["ms"]) / 1000, (sent + back) / 2)
    _skew_at = back


def server_now() -> float | None:
    """The server's clock (epoch seconds) as last measured; None before that."""
    if _server_anchor is None:
        return None
    server_s, mono = _server_anchor
    return server_s + (time.monotonic() - mono)


@contextlib.contextmanager
def under_lease(ends_at: float) -> Iterator[None]:
    """Every insert in this block starts by `ends_at` (monotonic: the publisher's
    deadline, at or before its lease's end) plus the transit allowance, however long it
    waits to be sent; one not carried is sent guarded all the same."""
    token = _lease_end.set(ends_at)
    try:
        yield
    finally:
        _lease_end.reset(token)


def sent_under_lease() -> bool:
    return _lease_end.get() is not None


async def _refresh_skew(ch: Any) -> None:
    if time.monotonic() - _skew_at > _SKEW_REFRESH_S:
        with contextlib.suppress(Exception):  # the last measure stays
            await _measure_skew(ch)


async def measure_server_clock(ch: Any) -> None:
    """Measure the server's clock when this process has not measured it lately (the
    watcher that keeps it fresh starts with a carried insert)."""
    if _server_anchor is None:
        await _measure_skew(ch)
    await _refresh_skew(ch)


async def measured_send_deadline(ch: Any) -> float:
    """`send_deadline`, the server's clock measured first (`measure_server_clock`)."""
    await measure_server_clock(ch)
    return send_deadline()


def send_deadline() -> float:
    """The latest server time a batch sent now may start at: the transit allowance
    past now, or past the end of the lease it is sent under if that is earlier."""
    now = server_now()
    if now is None:
        raise BackendUnavailableError("rmt carry: the server's clock is not measured yet; retry")
    lease = _lease_end.get()
    start = now if lease is None else min(now, now + lease - time.monotonic())
    return start + _TRANSIT_S


def _processes_sql(source: str) -> str:
    return f"SELECT count() AS n FROM {source} WHERE query_id IN {{ids:Array(String)}}"


async def _still_running(ch: Any, ids: list[str]) -> int | None:
    """How many of `ids` run on any replica; None when that cannot be read."""
    from server.core.ch_errors import error_code
    from server.settings import settings

    cluster = settings.ch_cluster
    sources = [f"clusterAllReplicas('{cluster}', system.processes)"] if cluster else []
    for source in [*sources, "system.processes"]:
        try:
            row = await ch.query_one(_processes_sql(source), {"ids": ids})
        except Exception as e:  # noqa: BLE001 — only a missing cluster asks the local server
            if error_code(e) == _CLUSTER_DOESNT_EXIST:
                continue
            return None
        return int(row["n"]) if row is not None else None
    return None


# A carried insert of another schema on the service never acknowledges in this one.
_WRITERS_SQL = (
    "SELECT DISTINCT splitByChar('/', query_id)[2] AS process FROM {source} "
    "WHERE startsWith(query_id, {{prefix:String}}) AND current_database = {{db:String}}"
)


async def _running_writers(ch: Any) -> set[str] | None:
    """The processes with a carried insert running on any replica; None when
    that cannot be read."""
    from server.core.ch_errors import error_code
    from server.settings import settings

    cluster = settings.ch_cluster
    sources = [f"clusterAllReplicas('{cluster}', system.processes)"] if cluster else []
    for source in [*sources, "system.processes"]:
        try:
            rows = await ch.query_all(
                _WRITERS_SQL.format(source=source),
                {"prefix": CARRY_QUERY_PREFIX, "db": database()},
            )
        except Exception as e:  # noqa: BLE001 — only a missing cluster asks the local server
            if error_code(e) == _CLUSTER_DOESNT_EXIST:
                continue
            return None
        return {str(r["process"]) for r in rows}
    return None


async def _kill(ch: Any, ids: list[str]) -> None:
    from server.settings import settings

    cluster = settings.ch_cluster
    on = f" ON CLUSTER '{cluster}'" if cluster else ""
    for sql in dict.fromkeys(
        (
            f"KILL QUERY{on} WHERE query_id IN {{ids:Array(String)}} SYNC",
            "KILL QUERY WHERE query_id IN {ids:Array(String)} SYNC",
        )
    ):
        try:
            await ch.execute(sql, {"ids": ids})
            return
        except Exception as e:  # noqa: BLE001 — the count below decides; a later settle kills again
            log.debug("rmt carry: kill of %s failed (%s)", ids, e)


async def _settle(ch: Any) -> None:
    """Release each pinned write no query of which can still run or start: past
    its release time, or, once its start deadline has passed, killed and counted
    gone on every replica."""
    for pin in list(_pinned):
        now = server_now()
        done = now is not None and now >= pin.release_at
        if not done:
            await _kill(ch, pin.query_ids)
            if now is not None and now > pin.deadline:
                running = await _still_running(ch, pin.query_ids)
                done = running == 0
                if running is None:
                    pin.error = "system.processes cannot be read"
                    if not pin.warned:
                        pin.warned = True
                        log.warning(
                            "rmt carry: queries %s of a cut-off write: %s; held until %.0f",
                            pin.query_ids,
                            pin.error,
                            pin.release_at,
                        )
        if done and pin in _pinned:  # the watcher and a kicked settle may both get here
            _pinned.remove(pin)
            _release(pin.gen)


def unsettled() -> int:
    """Carried writes of this process still in flight or pinned: an insert that may
    still run or start on the server."""
    return sum(_inflight.values())


async def settle(ch: Any) -> None:
    """Release the pinned writes that can no longer land (`_settle`), now."""
    if _pinned:
        await _settle(ch)


def pinned_overdue() -> list[dict[str, Any]]:
    """Pinned writes past their release time (the settle is not running), or whose
    queries cannot be counted, for /v1/health."""
    now = server_now()
    return [
        {"query_ids": list(pin.query_ids), "error": pin.error or "past its release time"}
        for pin in _pinned
        if pin.error or (now is not None and now > pin.release_at + _RELEASE_MARGIN_S)
    ]


_SETTLING: asyncio.Task[None] | None = None


def _kick_settle() -> None:
    """Settle soon rather than at the watcher's next poll."""
    global _SETTLING
    if _watch_ch is None or (_SETTLING is not None and not _SETTLING.done()):
        return
    with contextlib.suppress(RuntimeError):  # no running loop: the watcher settles
        _SETTLING = asyncio.get_running_loop().create_task(
            _settle(_watch_ch), name="rmt-carry-settle", context=contextvars.Context()
        )


async def await_acks(ch: Any, *, timeout_s: float | None = None) -> None:
    """Return once every other live process carries by this schema's column
    lists, and no process without a current acknowledgement (one killed with an
    insert in flight) still has a carried insert running on the server;
    RuntimeError naming the ones that do not, past `timeout_s` (by default long
    enough to outwait a dead writer's last insert: `_writer_bound_s`)."""
    if timeout_s is None:
        timeout_s = _boot_wait_s()

    from server.core.ch import sequential_reads

    deadline = time.monotonic() + timeout_s
    me = process_id()
    behind: list[str] = []
    waited = False
    while True:
        try:
            with sequential_reads():
                acks = await ch.query_all(
                    f"SELECT process, fingerprints, dateDiff('second', at, now64(3)) AS age "
                    f"FROM {ACK_TABLE} FINAL "
                    "WHERE at > now64(3) - toIntervalSecond({recent:UInt32}) "
                    "AND process != {me:String}",
                    {"recent": math.ceil(_writer_bound_s()), "me": me},
                )
                live = [a for a in acks if int(a["age"]) < _ACK_LIVE_S]
                claimed = {str(a["process"]): json.loads(str(a["fingerprints"])) for a in live}
                tables = {t for prints in claimed.values() for t in prints}
                current = {t: fingerprint(c) for t, c in (await _stored(ch, tables)).items()}
                writers = await _running_writers(ch)
        except Exception as e:  # noqa: BLE001 — retried until the deadline
            log.warning("rmt carry: acknowledgements unreadable (%s)", e)
        else:
            ages = {str(a["process"]): int(a["age"]) for a in acks}
            stale = {
                p
                for p, prints in claimed.items()
                if any(current[t] != f for t, f in prints.items())
            }
            if writers is None:
                # Unreadable: any process gone within the bound may still have one
                # running (a withdrawn or idle one, "{}", has none).
                gone = {
                    str(a["process"])
                    for a in acks
                    if str(a["process"]) not in claimed and str(a["fingerprints"]) != "{}"
                }
            else:
                # A process that acknowledges no table (it withdrew) has no carried
                # insert of its own lists running; one that still does is waited for.
                gone = writers - {p for p, prints in claimed.items() if prints} - {me}
            behind = sorted(
                [f"{p} ({ages.get(p, 0)} s ago)" for p in stale]
                + [f"{p} (gone; a carried insert of it may still run)" for p in gone]
            )
            if not behind:
                return
            if not waited:
                waited = True
                log.info("rmt carry: waiting for %s", ", ".join(behind))
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"after {timeout_s:.0f} s these processes still carry by an older schema, "
                "or left a carried insert running (or the acknowledgements could not be "
                f"read): {', '.join(behind) or '?'}. A version write there would drop a new "
                "column: stop them, then restart this process (it proceeds once their "
                f"acknowledgement is {_ACK_LIVE_S} s old and none of their inserts runs)"
            )
        await asyncio.sleep(_POLL_S / 2)


async def withdraw(ch: Any) -> None:
    """At shutdown, after the last write and the settles shutdown made: this process
    carries no table, so no boot waits for it. A write still in flight or pinned keeps
    the last acknowledgement instead, which then expires: a boot waits for it, and for
    its queries in `system.processes`, as for a process that died."""
    if _watcher is not None:
        _watcher.cancel()
    if _published is None:
        return
    if _inflight:
        log.warning(
            "rmt carry: not withdrawn, %d carried writes unsettled", sum(_inflight.values())
        )
        return
    with contextlib.suppress(Exception):
        await ch.insert(
            ACK_TABLE,
            [{"process": process_id(), "fingerprints": "{}"}],
            settings=async_insert_settings(),
        )


async def _schema_stamp(ch: Any) -> tuple[Any, ...] | None:
    """What changes when a column is added: the ledger, or a carried table's
    metadata time."""
    try:
        row = await ch.query_one(
            """
            SELECT (SELECT count() FROM loom._migrations) AS n,
                   (SELECT max(applied_at) FROM loom._migrations) AS at,
                   (SELECT max(metadata_modification_time) FROM system.tables
                    WHERE (database, name) IN {keys:Array(Tuple(String, String))}) AS altered
            """,
            {"keys": [split_table(table) for table in _TABLES]},
        )
    except Exception as e:  # noqa: BLE001 — the next poll tries again
        log.debug("rmt carry: schema unreadable (%s)", e)
        return None
    row = row or {}
    return row.get("n"), row.get("at"), row.get("altered")


async def _watch(ch: Any) -> None:
    """Re-read the column lists whenever the schema changes."""
    from server.core.ch import set_query_tag

    set_query_tag("rmt_carry.columns")
    # None: the first poll always re-reads, so an ALTER that landed between the
    # first read and this stamp is not taken as seen.
    seen: tuple[Any, ...] | None = None
    while True:
        await asyncio.sleep(_POLL_S)
        stamp = await _schema_stamp(ch)
        if stamp is not None and (stamp != seen or not _read_ok):
            if await _read_columns(ch):
                seen = stamp
        await _refresh_skew(ch)
        if _pinned:
            await _settle(ch)
        await _publish(ch)


def _start_watcher(ch: Any) -> None:
    global _watcher, _watch_ch
    _watch_ch = ch
    if _watcher is None or _watcher.done():
        # A fresh context: the task outlives the request that started it.
        _watcher = asyncio.get_running_loop().create_task(
            _watch(ch), name="rmt-carry-columns", context=contextvars.Context()
        )


async def unknown_columns(ch: Any, table: str) -> list[str]:
    """Stored columns of `table` its builder does not write. BackendUnavailable
    while they cannot be read: a version row written blind could drop one."""
    t = _TABLES[table]
    _start_watcher(ch)  # it retries a failed read or publish in the background
    await _admit(ch, t)
    if t.stored is None:
        raise BackendUnavailableError(f"the columns of {table} cannot be read yet; retry")
    write = _carried.get()
    if write is not None:
        write.taken[table] = t.stored
    return sorted(t.stored - t.known)


def invalidate(table: str) -> None:
    """The server refused a write to `table` for lists it no longer matches: the
    next write reads them again (and re-acknowledges) before it sends."""
    t = _TABLES.get(table)
    if t is not None:
        t.stored = None


def fence_columns(table: str) -> frozenset[str] | None:
    """The stored columns this carried write's rows were built against (None
    outside `writing()`): its insert lands only while the table has no other."""
    write = _carried.get()
    return None if write is None else write.taken.get(table)


async def carry_unknown_columns(
    ch: Any, table: str, rows: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
    """`rows` as the batches to insert. A row whose key has a stored version
    takes every unknown column of `table` from it; a new key's row leaves them
    out, so ClickHouse computes their defaults (one naming another column too)."""
    unknown = [c for c in await unknown_columns(ch, table) if rows and c not in rows[0]]
    if not unknown:
        return [rows]
    for c in unknown:
        if (table, c) not in _WARNED:
            _WARNED.add((table, c))
            log.warning("rmt carry: %s has column %r this build does not know", table, c)
    t = _TABLES[table]
    stored = await _newest_versions(ch, table, t, unknown, rows)
    carried = [
        {**row, **{c: src[c] for c in unknown}}
        for row in rows
        if (src := stored.get(t.key_of(row))) is not None
    ]
    fresh = [row for row in rows if t.key_of(row) not in stored]
    if not _fresh():
        # The lookup outlived the acknowledgement: these lists may be behind a boot.
        raise BackendUnavailableError(
            f"rmt carry: {table} write outlived its acknowledgement; retry"
        )
    return [batch for batch in (carried, fresh) if batch]


def reset_for_tests() -> None:
    global _watcher, _gen, _published, _read_ok, _watch_ch, _SETTLING, _server_anchor, _skew_at
    if _watcher is not None:
        _watcher.cancel()
    _watcher = _watch_ch = _SETTLING = None
    _server_anchor, _skew_at = None, -math.inf
    _pinned.clear()
    for t in _TABLES.values():
        t.stored = None
    _WARNED.clear()
    _gen, _published, _read_ok = 0, None, False
    _inflight.clear()
    _prints.clear()


async def _newest_versions(
    ch: Any, table: str, t: _Table, cols: list[str], rows: list[dict[str, Any]]
) -> dict[tuple[str, ...], dict[str, Any]]:
    from server.core.ch import own_write_reads

    key_sql = ", ".join(t.key)
    # Compared as String: an Enum or LowCardinality key matches a String element.
    key_type = ", ".join("String" for _ in t.key)
    keys = list(dict.fromkeys(t.key_of(row) for row in rows))
    out: dict[tuple[str, ...], dict[str, Any]] = {}
    for i in range(0, len(keys), _LOOKUP_CHUNK):
        # Sequentially consistent: another replica may have just written the row.
        with own_write_reads():
            found = await ch.query_all(
                f"""
                SELECT {key_sql}, {", ".join(cols)} FROM {table}
                WHERE ({key_sql}) IN {{keys:Array(Tuple({key_type}))}}
                ORDER BY version DESC
                LIMIT 1 BY {key_sql}
                """,
                {"keys": keys[i : i + _LOOKUP_CHUNK]},
            )
        for r in found:
            out[t.key_of(r)] = r
    return out
