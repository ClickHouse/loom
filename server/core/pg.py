"""The one Postgres client, for the Postgres storage profile.

`LOOM_STORAGE_AUTHORITY=postgres` makes Postgres the authority for live memory:
every statement goes through this module's pool, opened at boot (`open_pool`),
or on first use in a process that has no boot (a CLI command), and closed at
shutdown. Each connection pins `synchronous_commit = on` and a statement timeout, so no
request holds a connection past its budget. A commit is acknowledged only as durably as
the server's standby configuration allows (`server.core.pg_profile` checks that at
boot): the statement timeout does not cover its standby wait, which ends at the
standbys' flush or at the client's command timeout, a cancel that leaves the commit on
the primary alone and raises. `transaction` reports it `outcome_unknown`; a single
statement outside one reports it as not committed, so it writes only what a failover
may lose: a lease, a claim, a counter.

A failure to reach Postgres answers 503 POSTGRES_UNAVAILABLE: with the authority
down, writes and authoritative reads fail closed, never fall back to ClickHouse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import weakref
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import Any
from uuid import UUID

from server.core import provider_process, retirement
from server.core.error_summary import error_summary
from server.core.errors import LoomServerError
from server.settings import settings

log = logging.getLogger(__name__)

_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = weakref.WeakKeyDictionary()
_opening: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)
_closing: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Task[Any]] = (
    weakref.WeakKeyDictionary()
)
_state_pid = os.getpid()


def _check_process() -> None:
    global _state_pid
    if _state_pid != os.getpid():
        if _pools or any(lock.locked() for lock in _opening.values()) or _closing:
            raise retirement.ProcessStopping("Inherited Postgres ownership cannot be reused")
        _state_pid = os.getpid()


def _check_unsealed(gate: retirement.RetirementGate) -> None:
    if gate.pg_sealed:
        raise retirement.ProcessStopping("Postgres application opening is permanently closed")


@contextmanager
def _opening_scope() -> Iterator[retirement.RetirementGate]:
    _check_process()
    gate = retirement.get_gate()
    _check_unsealed(gate)
    gate.active += 1
    try:
        yield gate
    finally:
        gate.active -= 1


def authority_state_present() -> bool:
    """Cached or unfinished authority ownership, never evidence of remote absence."""
    _check_process()
    return bool(_pools or any(lock.locked() for lock in _opening.values()) or _closing)


class PostgresUnavailableError(LoomServerError):
    """503 — Postgres, the memory authority, could not answer. Retry after
    `retry_after_s`. `outcome_unknown`: a commit was sent and its answer was
    lost, so the write may have landed; retry with the same idempotency key."""

    code = "POSTGRES_UNAVAILABLE"
    status_code = 503

    def __init__(self, message: str = "", *, outcome_unknown: bool = False) -> None:
        super().__init__(message, outcome_unknown=outcome_unknown, retry_after_s=1)


class PostgresRefusedError(LoomServerError):
    """500 — Postgres refused a statement with a data or constraint error. It names the
    SQLSTATE and the constraint, table or column, never the row or argument the driver's
    text quotes ("Failing row contains ..."), and fails the same way on a retry."""

    code = "POSTGRES_REFUSED"
    status_code = 500

    def __init__(self, e: BaseException) -> None:
        self.sqlstate: str | None = getattr(e, "sqlstate", None) or None
        named = [
            f"{part} {value}"
            for part in ("constraint", "table", "column")
            if (value := getattr(e, f"{part}_name", None))
        ]
        super().__init__(
            f"Postgres refused the statement: {type(e).__name__} {self.sqlstate or ''}".rstrip()
            + "".join(f", {n}" for n in named)
        )


def enabled() -> bool:
    return settings.storage_authority == "postgres"


def unstorable(val: Any) -> bool:
    """Whether `val` holds text Postgres cannot store: a NUL character, or a lone
    surrogate (UTF-8 has no encoding for it)."""
    if isinstance(val, str):
        if "\x00" in val:
            return True
        try:
            val.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(val, (list, tuple)):
        return any(unstorable(v) for v in val)
    if isinstance(val, dict):
        return any(unstorable(k) or unstorable(v) for k, v in val.items())
    return False


async def init_connection(conn: Any) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def open_pool() -> None:
    """Open this event loop's pool against LOOM_PG_DSN. Raises when Postgres does not
    answer: a process configured for the Postgres authority does not serve without it."""
    await _pool_for_loop()


class AuthorityMismatch(RuntimeError):
    """This replica's configuration contradicts the deployment's recorded authority."""


# (deployment id, epoch) from ClickHouse's record: a CLI process's first connection,
# pooled or dedicated, checks that loom.authority holds the same
# (`pg_profile.check_configured`).
_expected: tuple[str, int] | None = None


def expect_deployment(deployment: tuple[str, int] | None) -> None:
    """(deployment id, epoch) the next connection opened must find in `loom.authority`."""
    global _expected
    _expected = deployment


def same_deployment(held: Mapping[str, Any], recorded: Mapping[str, Any]) -> None:
    """Refuse when Postgres's record (`held`) and ClickHouse's name other deployments."""
    if (str(held["deployment_id"]), int(held["epoch"])) != (
        str(recorded["deployment_id"]),
        int(recorded["epoch"]),
    ):
        raise AuthorityMismatch(
            f"Postgres records deployment {held['deployment_id']} epoch {held['epoch']}, "
            f"ClickHouse deployment {recorded['deployment_id']} epoch {recorded['epoch']}: "
            "LOOM_PG_DSN and the ClickHouse settings name different deployments"
        )


async def verify_deployment() -> None:
    """Run the deployment check now when one is armed: for a command that writes to
    ClickHouse before its first Postgres statement."""
    if _expected is not None:
        try:
            pool = await _pool_for_loop()
            # Opening a pool runs the check and disarms it; one already open is checked here.
            if _expected is not None:
                async with _acquire(pool) as conn:
                    await _check_deployment(conn)
        except Exception as e:
            _raise_if_unavailable(e)
            raise


NO_DSN = "LOOM_STORAGE_AUTHORITY=postgres needs LOOM_PG_DSN"


def _require_dsn() -> None:
    if not settings.pg_dsn:
        raise RuntimeError(NO_DSN)


async def _check_deployment(conn: Any) -> None:
    global _expected
    if _expected is None:
        return
    import asyncpg

    try:
        held = await conn.fetchrow("SELECT epoch, deployment_id FROM loom.authority")
    except asyncpg.exceptions.UndefinedTableError:
        held = None  # not migrated: it records no authority
    if held is None:
        raise AuthorityMismatch("LOOM_PG_DSN names a database that records no authority")
    deployment_id, epoch = _expected
    same_deployment(held, {"deployment_id": deployment_id, "epoch": epoch})
    _expected = None  # one DSN per process: checked once


async def _pool_connect(*args: Any, **kwargs: Any) -> Any:
    """Pool maintenance owns opening and codecs until the connection is usable."""
    import asyncpg

    with _opening_scope() as gate:
        conn = await asyncpg.connect(*args, **kwargs)
        try:
            await init_connection(conn)
            _check_unsealed(gate)
            return conn
        except BaseException:
            with suppress(BaseException):
                _terminate(conn)
            raise


async def _pool_for_loop() -> Any:
    """Construct and publish a pool only while this process can open application work."""
    import asyncpg

    with _opening_scope() as gate:
        loop = asyncio.get_running_loop()
        pool = _pools.get(loop)
        if pool is not None:
            return pool
        _require_dsn()
        lock = _opening.setdefault(loop, asyncio.Lock())
        async with lock:
            _check_unsealed(gate)
            if loop not in _pools:
                pool = await asyncpg.create_pool(
                    settings.pg_dsn,
                    min_size=settings.pg_pool_min_size,
                    max_size=settings.pg_pool_max_size,
                    connect=_pool_connect,
                    server_settings={
                        "application_name": provider_process.participant_tag(
                            provider_process.process_identity()
                        ),
                        "synchronous_commit": "on",
                        "default_transaction_isolation": "read committed",
                        "statement_timeout": str(settings.pg_statement_timeout_ms),
                        "idle_in_transaction_session_timeout": idle_timeout_ms(),
                        **marker_settings(),
                    },
                    command_timeout=settings.pg_statement_timeout_ms / 1000 + 1,
                )
                try:
                    _check_unsealed(gate)
                    if _expected is not None:
                        async with _acquire(pool) as conn:
                            await _check_deployment(conn)
                    _check_unsealed(gate)
                except BaseException:
                    with suppress(BaseException):
                        pool.terminate()
                    raise
                _pools[loop] = pool
            return _pools[loop]


@asynccontextmanager
async def _acquire(pool: Any) -> AsyncIterator[Any]:
    with _opening_scope() as gate:
        try:
            conn = await pool.acquire(timeout=settings.pg_statement_timeout_ms / 1000)
        except Exception as error:
            _raise_if_unavailable(error)
            raise
        try:
            _check_unsealed(gate)
        except BaseException:
            with suppress(BaseException):
                _terminate(conn)
            with suppress(BaseException):
                await _release(pool, conn)
            raise
    try:
        yield conn
    finally:
        await _release(pool, conn)


def idle_timeout_ms() -> str:
    """How long the server keeps a transaction a client left idle (a cancelled request, a
    stalled process) before it ends it and releases its locks."""
    return str(settings.pg_statement_timeout_ms * 2)


def _pool_close_task() -> tuple[Any, asyncio.Task[Any]] | None:
    _check_process()
    loop = asyncio.get_running_loop()
    if any(owner is not loop for owner in (*_pools, *_closing)) or any(
        owner is not loop and lock.locked() for owner, lock in _opening.items()
    ):
        raise retirement.ProcessStopping("Another event loop still owns Postgres state")
    pool = _pools.get(loop)
    if pool is None:
        return None
    task = _closing.get(loop)
    if task is None:
        gate = retirement.get_gate()
        gate.active += 1

        async def close() -> SystemExit | KeyboardInterrupt | None:
            try:
                await pool.close()
            except (SystemExit, KeyboardInterrupt) as error:
                return error
            return None

        def ended(task: asyncio.Task[Any]) -> None:
            gate.active -= 1
            _retrieve_task(task)

        task = asyncio.create_task(close())
        task.add_done_callback(ended)
        _closing[loop] = task
    return pool, task


def _forget_pool() -> None:
    loop = asyncio.get_running_loop()
    _pools.pop(loop, None)
    _closing.pop(loop, None)
    _opening.pop(loop, None)


async def close_pool() -> None:
    """Reversible close; retain an unfinished or failed close for its next owner."""
    owned = _pool_close_task()
    if owned is not None:
        pool, task = owned
        try:
            result = await asyncio.shield(task)
            if isinstance(result, (SystemExit, KeyboardInterrupt)):
                raise result
        except BaseException:
            if task.done() and not retirement.get_gate().pg_sealed:
                with suppress(BaseException):
                    pool.terminate()
                _forget_pool()
            raise
        else:
            if not retirement.get_gate().pg_sealed:
                _forget_pool()


async def close_pool_until(*, end: float) -> bool:
    """The terminal owner keeps one close child through its actual completion."""
    owned = _pool_close_task()
    if owned is None:
        return True
    pool, task = owned
    if task.done() and task.cancelled():
        return True
    try:
        await _wait_owned(task, end=end, abort=pool.terminate)
    except (SystemExit, KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 — only a fresh remote census can resolve a failed close.
        return task.done()
    return True


def finish_retirement() -> None:
    """Positive remote absence resolved the retained, completed local pool owner."""
    _forget_pool()


# Errors a retry may clear, by SQLSTATE: a cancelled statement (its timeout, a lock it
# waited for too long), a deadlock, a serialization failure, a lock not available, a
# read-only node (a connection left on a demoted primary).
_RETRYABLE_SQLSTATES = frozenset({"57014", "40P01", "40001", "55P03", "25006"})
# The server short of a resource, which a retry may find again: disk full, out of memory,
# too many connections, a system error, an I/O error. Not a configuration limit or a
# missing file, which fail the same way every time.
_RESOURCE_SQLSTATES = frozenset({"53100", "53200", "53300", "58000", "58030"})
_TRANSIENT_SQLSTATES = _RETRYABLE_SQLSTATES | _RESOURCE_SQLSTATES


def is_outage(e: BaseException) -> bool:
    """A lost or refused connection, a retryable SQLSTATE, or a server in recovery (a
    demoted primary, `_in_recovery`). Not an InterfaceError: a bad argument or a misuse of
    the connection fails the same way on a retry."""
    import asyncpg

    sqlstate = getattr(e, "sqlstate", None)
    if sqlstate in _TRANSIENT_SQLSTATES or _in_recovery(e):
        return True
    if isinstance(e, asyncpg.exceptions.ProtocolViolationError):
        return False  # 08P01: a retry repeats a protocol violation
    return isinstance(
        e,
        (
            OSError,  # TimeoutError and the connection errors included
            asyncpg.exceptions.PostgresConnectionError,  # SQLSTATE class 08
            asyncpg.exceptions.CannotConnectNowError,
            asyncpg.exceptions.AdminShutdownError,
            asyncpg.exceptions.CrashShutdownError,
            # An unexpected driver state (a protocol caught mid-switch by a connection the
            # server ended, a stale schema cache): never a caller's misuse, which is an
            # InterfaceError.
            asyncpg.exceptions.InternalClientError,
            # A result type a later migration changed under a prepared statement: the
            # retry prepares it again.
            asyncpg.exceptions.InvalidCachedStatementError,
        ),
    )


def _raise_if_unavailable(
    e: Exception, *, outcome_unknown: bool = False, lost: bool = False
) -> None:
    """503 TENANCY_REFUSED for a refusal by a cell's org triggers or its `loom.tenancy`
    guard (SQLSTATE LT001); 503 POSTGRES_UNAVAILABLE for an error a retry may clear, or any
    error once the connection was `lost` (the driver's error then depends on when it
    noticed); a data or constraint error, whose text quotes the row or the argument, as
    PostgresRefusedError; otherwise the caller re-raises `e` as it is."""
    # The driver's own argument error lives in its private module: the public
    # `asyncpg.exceptions.DataError` is the server's SQLSTATE 22000.
    from asyncpg.exceptions._base import DataError as ClientDataError

    if isinstance(e, LoomServerError):
        return  # already the answer, a refusal raised in the block included
    _raise_if_tenancy_refused(e)
    if lost or is_outage(e):
        raise PostgresUnavailableError(
            "the commit's answer was lost" if outcome_unknown else "Postgres did not answer",
            outcome_unknown=outcome_unknown,
        ) from e
    if isinstance(e, ClientDataError) or (getattr(e, "sqlstate", None) or "").startswith(
        ("22", "23")
    ):
        raise PostgresRefusedError(e) from None


# The SQLSTATE of the org triggers and the `loom.tenancy` guard (migrations
# schemas/pg_migrations/011_tenancy_triggers.sql and 012_tenancy_guard.sql).
TENANCY_REFUSED_SQLSTATE = "LT001"


def _raise_if_tenancy_refused(e: BaseException) -> None:
    if getattr(e, "sqlstate", None) == TENANCY_REFUSED_SQLSTATE:
        from server.core.errors import TenancyRefusedError

        # The function's text names the refused row's org (for the `loom.tenancy` guard, the
        # org the call acts in) and nothing else a row carries: logged with its table, never
        # answered.
        log.warning(
            "Postgres refused a row of loom.%s from an unmarked session: %s",
            getattr(e, "table_name", None) or "?",
            getattr(e, "message", None) or type(e).__name__,
        )
        raise TenancyRefusedError(
            "this cell's Postgres refused a write from a session without the tenancy "
            "marker; see loom doctor, or run with LOOM_CELL set"
        ) from None


# The setting that marks a session as one that ran the tenancy check: the WHEN of the org
# triggers and the `loom.tenancy` guard skips such a session (`server.core.cell_triggers`;
# its name never changes, rolling-upgrades rule 47).
TENANCY_MARKER = "loom.tenancy_checked"


def marker_settings() -> dict[str, str]:
    """The startup parameter that marks a session of a process with LOOM_CELL set as one
    that ran the tenancy check: it survives the pool's RESET ALL. A process with
    LOOM_CELL unset sends none."""
    return {TENANCY_MARKER: "on"} if settings.cell else {}


# not a setting: a bound on returning a connection to the pool, not on a request
_RELEASE_TIMEOUT_S = 1.0


@asynccontextmanager
async def connection() -> AsyncIterator[Any]:
    """One pooled connection for the block; an unreachable server is 503."""
    try:
        pool = await _pool_for_loop()
    except Exception as e:
        _raise_if_unavailable(e)
        raise
    async with _acquire(pool) as conn:
        try:
            yield conn
        except BaseException as e:
            if _read_only(e):
                _terminate(conn)
                await pool.expire_connections()
            elif _connection_lost(e):
                _terminate(conn)
            raise


def _lost(conn: Any) -> bool:
    """Whether the server or the network ended `conn`: closed, or detached from the
    pool by the driver, which makes any call on it raise an InterfaceError."""
    import asyncpg

    try:
        return bool(conn.is_closed())
    except asyncpg.InterfaceError:
        return True


def _terminate(conn: Any) -> None:
    """Close a pooled connection now. One whose connection was lost is already detached
    from the pool, and a call on it would replace the block's error."""
    import asyncpg

    with suppress(asyncpg.InterfaceError):
        conn.terminate()


def _retrieve_task(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _wait_owned(task: asyncio.Task[Any], *, end: float, abort: Callable[[], None]) -> Any:
    interrupted: asyncio.CancelledError | None = None
    try:
        while not task.done():
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Postgres owned cleanup deadline expired")
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError as error:
                interrupted = interrupted or error
        result = task.result()
        if isinstance(result, (SystemExit, KeyboardInterrupt)):
            raise result
    except BaseException:
        try:
            abort()
        except BaseException:  # noqa: BLE001 — abort preserves the first cleanup failure.
            pass
        if not task.done():
            task.cancel()
        task.add_done_callback(_retrieve_task)
        if interrupted is not None:
            raise interrupted from None
        raise
    if interrupted is not None:
        raise interrupted
    return result


async def close_owned(conn: Any, *, end: float) -> None:
    """Bound public close; local abort is not proof the backend released its locks."""
    if end <= time.monotonic():
        with suppress(BaseException):
            _terminate(conn)
        raise TimeoutError("Postgres owned-session cleanup deadline expired")

    async def close() -> SystemExit | KeyboardInterrupt | None:
        try:
            await conn.close(timeout=max(0, end - time.monotonic()))
        except (SystemExit, KeyboardInterrupt) as error:
            return error
        return None

    await _wait_owned(asyncio.create_task(close()), end=end, abort=lambda: _terminate(conn))


async def _release(pool: Any, conn: Any) -> None:
    """Back to the pool within a bound: a statement still in flight (a client timeout, a
    cancelled request) has its cancel sent and acknowledged and the connection reset;
    asyncpg closes one that does not finish in time. A failure there never changes the
    block's outcome; a cancel is raised (`transaction` marks one after its COMMIT). The
    connection is not closed at once: closing it would also stop the cancel, leaving the
    server running the statement with its locks held."""
    import asyncpg

    try:
        await pool.release(conn, timeout=_RELEASE_TIMEOUT_S)
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        log.warning("Postgres connection not reset (%s); closed", error_summary(e))


def _read_only(e: BaseException) -> bool:
    """A statement refused by a server in recovery (a demoted primary): a write's 25006,
    or a durable read's logical message, which cannot take an xid there (XX000)."""
    return any(_in_recovery(x) for x in (e, e.__cause__) if x is not None)


def _in_recovery(e: BaseException) -> bool:
    sqlstate = getattr(e, "sqlstate", None)
    return sqlstate == "25006" or (
        sqlstate == "XX000" and "cannot assign TransactionIds during recovery" in str(e)
    )


def _connection_lost(e: BaseException) -> bool:
    """The connection itself is gone (not a timeout: a timed-out statement's cancel is
    still to be sent on it)."""
    import asyncpg

    for err in (e, e.__cause__):
        if isinstance(err, asyncpg.exceptions.ConnectionDoesNotExistError):
            return True
        if isinstance(err, OSError) and not isinstance(err, TimeoutError):
            return True
    return False


def may_have_committed(e: BaseException) -> bool:
    """Whether a write that ended in `e` may have committed: its commit's answer was lost,
    or the task was cancelled from its COMMIT on, before the caller heard the commit's
    answer (`mark_commit_in_flight`). A task cancelled inside a `transaction` block was
    rolled back."""
    if isinstance(e, asyncio.CancelledError):
        return bool(getattr(e, "commit_in_flight", False))
    return isinstance(e, PostgresUnavailableError) and bool(e.extra.get("outcome_unknown"))


async def _connect_dedicated(
    *,
    statement_timeout_ms: int = 0,
    marked: bool = True,
    bind: Callable[[Any], bool] | None = None,
) -> Any:
    """Initialize a session; an explicit owner binds before initialization can wait.

    A successful synchronous bind transfers cleanup, including when it returns False
    to refuse initialization. A bind that raises leaves cleanup with this connector.
    """
    import asyncpg

    with _opening_scope() as gate:
        _require_dsn()
        conn = None
        owned = False
        try:
            conn = await asyncpg.connect(
                settings.pg_dsn,
                command_timeout=None,
                server_settings={
                    "application_name": provider_process.participant_tag(
                        provider_process.process_identity()
                    ),
                    "synchronous_commit": "on",
                    "default_transaction_isolation": "read committed",
                    "statement_timeout": str(statement_timeout_ms),
                    "idle_in_transaction_session_timeout": idle_timeout_ms(),
                    **(marker_settings() if marked else {}),
                },
            )
            if bind is not None:
                usable = bind(conn)
                owned = True
                if not usable:
                    raise retirement.ProcessStopping("Postgres connection owner is closed")
            _check_unsealed(gate)
            await _check_deployment(conn)
            await init_connection(conn)
            _check_unsealed(gate)
            return conn
        except BaseException as error:
            if conn is not None and not owned:
                with suppress(BaseException):
                    _terminate(conn)
            if isinstance(error, Exception):
                _raise_if_unavailable(error)
            raise


async def retirement_settled(
    participant: UUID, recorded: Mapping[str, Any], *, legacy: bool, end: float
) -> bool:
    """Only fixed metadata may connect beyond the terminal application seal."""
    import asyncpg

    _check_process()
    _require_dsn()
    conn = None

    def remaining() -> float:
        value = end - time.monotonic()
        if value <= 0:
            raise TimeoutError("Postgres retirement proof deadline expired")
        return value

    async def verify() -> bool | SystemExit | KeyboardInterrupt:
        nonlocal conn
        try:
            budget = remaining()
            conn = await asyncpg.connect(
                settings.pg_dsn,
                timeout=budget,
                command_timeout=budget,
                server_settings={
                    "application_name": provider_process.participant_tag(
                        provider_process.process_identity()
                    ),
                    "synchronous_commit": "on",
                    "default_transaction_isolation": "read committed",
                    "statement_timeout": str(
                        min(settings.pg_statement_timeout_ms, max(1, int(budget * 1000)))
                    ),
                    "idle_in_transaction_session_timeout": idle_timeout_ms(),
                },
            )
            try:
                held = await conn.fetchrow(
                    "SELECT authority, deployment_id, epoch, pg_is_in_recovery() AS in_recovery "
                    "FROM loom.authority",
                    timeout=remaining(),
                )
                if (
                    held is None
                    or held.get("authority") != "postgres"
                    or str(held.get("deployment_id")) != str(recorded["deployment_id"])
                    or type(held.get("epoch")) is not int
                    or held["epoch"] != recorded["epoch"]
                    or type(held.get("in_recovery")) is not bool
                    or held["in_recovery"]
                ):
                    return False
                count = await conn.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
                    "AND (application_name = $1 OR ($2::boolean AND application_name = 'loom'))",
                    provider_process.participant_tag(participant),
                    legacy,
                    timeout=remaining(),
                )
                return type(count) is int and count == 0
            finally:
                await close_owned(conn, end=end)
        except (SystemExit, KeyboardInterrupt) as error:
            return error

    task = asyncio.create_task(verify())
    try:
        return (
            await _wait_owned(
                task, end=end, abort=lambda: _terminate(conn) if conn is not None else None
            )
            is True
        )
    except (SystemExit, KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 — inaccessible or unclosed evidence cannot certify absence.
        return False


@asynccontextmanager
async def dedicated(*, statement_timeout_ms: int = 0, marked: bool = True) -> AsyncIterator[Any]:
    """A connection of its own, outside the pool, with no client timeout and no server
    statement timeout unless `statement_timeout_ms` asks for one: for work that runs as
    long as it takes (migrations, an erasure's deletes).
    A CLI process's first one checks the deployment (`expect_deployment`).
    `statement_timeout_ms` bounds its statements as a startup value (a RESET ALL keeps
    it); `marked` False leaves the tenancy marker out (`loom doctor` reads a session
    without it)."""
    import asyncpg

    conn = await _connect_dedicated(statement_timeout_ms=statement_timeout_ms, marked=marked)
    try:
        try:
            yield conn
        except Exception as e:
            # The block's errors pass through as they are, but for the org triggers'
            # refusal, which every caller sees as the same 503 TENANCY_REFUSED.
            _raise_if_tenancy_refused(e)
            raise
    finally:
        # Bounded as `_release` is; a close that fails never replaces the block's outcome.
        try:
            await conn.close(timeout=_RELEASE_TIMEOUT_S)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            log.warning("Postgres connection not closed cleanly (%s); aborted", error_summary(e))


@asynccontextmanager
async def transaction() -> AsyncIterator[Any]:
    """A read-committed transaction on one connection. An error a retry may clear is
    503 POSTGRES_UNAVAILABLE: raised in the block, nothing was committed; raised by
    the commit (`outcome_unknown`), the commit may have landed. A cancel from the
    COMMIT on, the connection's release included, is marked `commit_in_flight`: the
    caller has not heard the commit's answer."""
    committing = False
    try:
        async with connection() as conn:
            tx = conn.transaction()
            try:
                await tx.start()
            except Exception as e:
                # Not rolled back here: the pool's reset ends a BEGIN whose answer was lost.
                _raise_if_unavailable(e)
                raise
            try:
                yield conn
            except BaseException as e:
                lost = _lost(conn)
                await _rollback(conn, tx)
                if isinstance(e, Exception):
                    _raise_if_unavailable(e, lost=lost)
                raise
            # The standby wait is outside the statement timeout (the module docstring).
            committing = True
            try:
                await tx.commit()
            except Exception as e:
                _raise_if_unavailable(e, outcome_unknown=True, lost=_lost(conn))
                raise
    except asyncio.CancelledError as e:
        if committing:
            mark_commit_in_flight(e)
        raise


# A transactional logical message: the commit that carries it writes WAL, so it waits
# for the synchronous standbys even when the transaction wrote nothing else.
_DURABLE_MESSAGE = "SELECT pg_logical_emit_message(true, 'loom.durable', '')"


@asynccontextmanager
async def durable_transaction() -> AsyncIterator[Any]:
    """`transaction`, for a read of other sessions' commits whose rows go to ClickHouse:
    once it returns, every row the block read is on the synchronous standbys. A row is
    visible only after its own commit's standby wait ended (a cancelled wait leaves it on
    the primary alone), so it was committed before this transaction's commit, which
    writes a logical message and waits for the standbys. The read and the commit share
    one connection: a failover in between fails the commit, never another primary's. A
    cancelled wait raises (`outcome_unknown`): nothing read may be sent. A claim needs no
    message: an UPDATE that claimed rows wrote WAL, and one that claimed none has nothing
    to send."""
    async with transaction() as conn:
        yield conn
        await conn.execute(_DURABLE_MESSAGE)


async def check_durable_message() -> None:
    """Write the logical message `durable_transaction` commits, in a transaction rolled
    back so nothing waits for a standby: raises for a role that may not."""
    async with rolled_back() as conn:
        await conn.execute(_DURABLE_MESSAGE)


@asynccontextmanager
async def rolled_back() -> AsyncIterator[Any]:
    """A transaction always rolled back, for a read that wants only its locks' waits: a
    COMMIT of row locks would wait on the synchronous standby as a write's does."""
    async with connection() as conn:
        tx = conn.transaction()
        try:
            await tx.start()
        except Exception as e:
            # Not rolled back here: the pool's reset ends a BEGIN whose answer was lost.
            _raise_if_unavailable(e)
            raise
        try:
            yield conn
        except Exception as e:
            _raise_if_unavailable(e, lost=_lost(conn))
            raise
        finally:
            await _rollback(conn, tx)


def mark_commit_in_flight(e: asyncio.CancelledError) -> None:
    """Mark a cancel from a COMMIT on, before the caller heard its answer (in
    `transaction`, or while a writer read a lost commit back), for `may_have_committed`."""
    e.commit_in_flight = True  # type: ignore[attr-defined]


async def _rollback(conn: Any, tx: Any) -> None:
    """Roll back within `_RELEASE_TIMEOUT_S`, never hiding the error that ended the
    block: a statement still in flight is cancelled first, and one that does not finish
    in time leaves the connection to `_release`, which has a bound of its own."""
    if _lost(conn):
        return  # nothing to roll back; a detached one fails any call (`_terminate`)
    try:
        await asyncio.wait_for(tx.rollback(), _RELEASE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — the error that ended the block is raised
        log.warning(
            "Postgres rollback failed (%s); the pool resets the connection", error_summary(e)
        )


async def fetch(sql: str, *args: Any) -> list[Any]:
    async with connection() as conn:
        try:
            return list(await conn.fetch(sql, *args))
        except Exception as e:
            _raise_if_unavailable(e)
            raise


async def fetchrow(sql: str, *args: Any) -> Any:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


async def fetchval(sql: str, *args: Any) -> Any:
    row = await fetchrow(sql, *args)
    return row[0] if row else None


async def fetchrow_reset(sql: str) -> Any:
    """`fetchrow` after RESET ALL, in one transaction (`transaction`, with its rollback and
    error mapping), so it reads the session's settings as its startup values leave them:
    a SET is gone, and a pooler (which a cell must not have) cannot swap the server
    connection between the reset and the read. Three round trips more than `fetchrow`."""
    async with transaction() as conn:
        await conn.execute("RESET ALL")
        return await conn.fetchrow(sql)


async def execute(sql: str, *args: Any) -> str:
    async with connection() as conn:
        try:
            return str(await conn.execute(sql, *args))
        except Exception as e:
            _raise_if_unavailable(e)
            raise


# Seconds a holder of a delivery `d` may still send: its lease, or the last moment one
# that no longer holds it may (`fenced_until`); negative once both have passed. A parked
# delivery has no lease of its own, and always a `fenced_until`.
LEASE_LEFT_S = (
    "extract(epoch FROM GREATEST(NULLIF(d.lease_until, 'infinity'), d.fenced_until) - now())"
)


def insert_bound_s() -> float:
    """How long after a lease's end an insert sent under it can still land, which an
    erasure or a redaction waits out (`rmt_carry.send_bound_s`)."""
    from server.core import rmt_carry

    return rmt_carry.send_bound_s()


async def wait_out(leases_left_s: list[float]) -> None:
    """Return once no holder of these delivery leases (seconds left on each) can still
    land an insert in ClickHouse: what an erasure or a redaction waits for before it
    cleans ClickHouse's copies."""
    if not leases_left_s:
        return
    wait = max(leases_left_s) + insert_bound_s()
    if wait > 0:
        log.info("waiting %.0f s for the publishers of removed deliveries to stop", wait)
        await asyncio.sleep(wait)


# The advisory lock of an org's idempotency keys (`$1`, the org): a request claiming a key
# tries it shared, and an erasure's idempotency step fences the org's claims under it alone.
_IDEMPOTENCY_LOCK_KEY = "hashtextextended('loom.idempotency/' || $1::text, 0)"


async def try_claim_lock(conn: Any, org: str) -> bool:
    """The org's idempotency lock, shared, to the end of the transaction; False while an
    erasure's step holds or waits for it."""
    sql = f"SELECT pg_try_advisory_xact_lock_shared({_IDEMPOTENCY_LOCK_KEY})"
    return bool(await conn.fetchval(sql, org))


async def fence_lock(conn: Any, org: str) -> None:
    """The org's idempotency lock alone, to the end of the transaction: no claim is made
    while the step holding it fences the org's claims."""
    await conn.execute(f"SELECT pg_advisory_xact_lock({_IDEMPOTENCY_LOCK_KEY})", org)


# The advisory lock of a namespace's knowledge documents (`$1` the org, `$2` the
# namespace): a document commit holds it shared, and an erasure of the namespace's
# documents alone, so no commit lands between the erasure's read and its delete.
KNOWLEDGE_LOCK_KEY = "hashtextextended('loom.knowledge/' || $1::text || '/' || $2::text, 0)"

# The advisory lock of a policy scope (`k.org`, `k.scope`), one key per scope.
POLICY_LOCK_KEY = "hashtextextended('loom.policy/' || k.org || '/' || k.scope, 0)"


async def bump_policy(org: str, scope: str = "*", *, deadline: float | None = None) -> None:
    """Advance a policy generation (org '' for the deployment's): an admission that
    reached its verdict under the old policy starts over (`pg_memory.commit_versions`).
    Fails closed: the change answers 503 until the generation moves, and its retry
    moves it."""
    if not enabled():
        return
    async with transaction() as conn:
        if deadline is not None:
            await _bounded_policy_statement(conn, deadline)
        # After every commit that holds the policy shared (`pg_memory.commit_versions`).
        await conn.execute(
            f"SELECT pg_advisory_xact_lock({POLICY_LOCK_KEY}) "
            "FROM (SELECT $1::text AS org, $2::text AS scope) AS k",
            org,
            scope,
        )
        if deadline is not None:
            await _bounded_policy_statement(conn, deadline)
        await conn.execute(
            "INSERT INTO loom.scope_generations (org, scope, generation) VALUES ($1, $2, 1) "
            "ON CONFLICT (org, scope) "
            "DO UPDATE SET generation = loom.scope_generations.generation + 1",
            org,
            scope,
        )


async def _bounded_policy_statement(conn: Any, deadline: float) -> None:
    remaining_ms = int(min(2.0, deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        raise PostgresUnavailableError("policy change allowance exhausted")
    await conn.execute(
        "SELECT set_config('statement_timeout', $1, true)",
        str(remaining_ms),
        timeout=remaining_ms / 1000,
    )
