"""The one Postgres client, for the Postgres storage profile.

`LOOM_STORAGE_AUTHORITY=postgres` makes Postgres the authority for live memory:
every statement goes through this module's pool, opened at boot (`open_pool`),
or on first use in a process that has no boot (a CLI command), and closed at
shutdown. Each connection pins `synchronous_commit = on` and a statement timeout, so no
request holds a connection past its budget. A commit is acknowledged only as durably as
the server's standby configuration allows (`server.core.pg_profile` checks that at
boot): the statement timeout does not cover its standby wait, which ends at the
standbys' flush or at the client's command timeout, a cancel that leaves the commit on
the primary alone and raises (`outcome_unknown` from `transaction`).

A failure to reach Postgres answers 503 POSTGRES_UNAVAILABLE: with the authority
down, writes and authoritative reads fail closed, never fall back to ClickHouse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from server.core.error_summary import error_summary
from server.core.errors import LoomServerError
from server.settings import settings

log = logging.getLogger(__name__)

_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = weakref.WeakKeyDictionary()
_opening: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


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
                async with pool.acquire() as conn:
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


async def _pool_for_loop() -> Any:
    """The pool of the running event loop, opened on first use: the server and worker
    processes open it at boot, a CLI command or a test on its first statement. A pool
    belongs to the loop that opened it; a CLI process's first one checks the deployment
    (`expect_deployment`)."""
    import asyncpg

    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is not None:
        return pool
    _require_dsn()
    async with _opening.setdefault(loop, asyncio.Lock()):
        if loop not in _pools:
            pool = await asyncpg.create_pool(
                settings.pg_dsn,
                min_size=settings.pg_pool_min_size,
                max_size=settings.pg_pool_max_size,
                init=init_connection,
                server_settings={
                    "application_name": "loom",
                    "synchronous_commit": "on",
                    # The commits re-check rows they waited on; whatever the server's default.
                    "default_transaction_isolation": "read committed",
                    "statement_timeout": str(settings.pg_statement_timeout_ms),
                    # A transaction left open by a cancelled request is ended by the server.
                    "idle_in_transaction_session_timeout": idle_timeout_ms(),
                },
                command_timeout=settings.pg_statement_timeout_ms / 1000 + 1,
            )
            if _expected is not None:
                try:
                    async with pool.acquire() as conn:
                        await _check_deployment(conn)
                except BaseException:
                    pool.terminate()  # never handed out: nothing to wait for
                    raise
            _pools[loop] = pool
    return _pools[loop]


def idle_timeout_ms() -> str:
    """How long the server keeps a transaction a client left idle (a cancelled request, a
    stalled process) before it ends it and releases its locks."""
    return str(settings.pg_statement_timeout_ms * 2)


async def close_pool() -> None:
    loop = asyncio.get_running_loop()
    _opening.pop(loop, None)
    pool = _pools.pop(loop, None)
    if pool is not None:
        await pool.close()


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
    """A lost or refused connection, or a retryable SQLSTATE. Not an InterfaceError: a
    bad argument or a misuse of the connection fails the same way on a retry."""
    import asyncpg

    sqlstate = getattr(e, "sqlstate", None)
    if sqlstate in _TRANSIENT_SQLSTATES:
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
            # A result type a later migration changed under a prepared statement: the
            # retry prepares it again.
            asyncpg.exceptions.InvalidCachedStatementError,
        ),
    )


def _raise_if_unavailable(e: Exception, *, outcome_unknown: bool = False) -> None:
    """503 POSTGRES_UNAVAILABLE for an error a retry may clear; a data or constraint
    error, whose text quotes the row or the argument, as PostgresRefusedError; otherwise
    the caller re-raises `e` as it is."""
    # The driver's own argument error lives in its private module: the public
    # `asyncpg.exceptions.DataError` is the server's SQLSTATE 22000.
    from asyncpg.exceptions._base import DataError as ClientDataError

    if isinstance(e, LoomServerError):
        return  # already the answer, a refusal raised in the block included
    if is_outage(e):
        raise PostgresUnavailableError(
            "the commit's answer was lost" if outcome_unknown else "Postgres did not answer",
            outcome_unknown=outcome_unknown,
        ) from e
    if isinstance(e, ClientDataError) or (getattr(e, "sqlstate", None) or "").startswith(
        ("22", "23")
    ):
        raise PostgresRefusedError(e) from None


# not a setting: a bound on returning a connection to the pool, not on a request
_RELEASE_TIMEOUT_S = 1.0


@asynccontextmanager
async def connection() -> AsyncIterator[Any]:
    """One pooled connection for the block; an unreachable server is 503."""
    try:
        pool = await _pool_for_loop()
        conn = await pool.acquire(timeout=settings.pg_statement_timeout_ms / 1000)
    except Exception as e:
        _raise_if_unavailable(e)
        raise
    try:
        yield conn
    except BaseException as e:
        if _read_only(e):
            # Left on a demoted primary: none of the pool's connections is handed out again.
            _terminate(conn)
            await pool.expire_connections()
        elif _connection_lost(e):
            _terminate(conn)
        raise
    finally:
        await _release(pool, conn)


def _terminate(conn: Any) -> None:
    """Close a pooled connection now. One whose connection was lost is already detached
    from the pool, and a call on it would replace the block's error."""
    import asyncpg

    try:
        conn.terminate()
    except asyncpg.InterfaceError:
        pass


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
    return "25006" in (getattr(e, "sqlstate", None), getattr(e.__cause__, "sqlstate", None))


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


@asynccontextmanager
async def dedicated() -> AsyncIterator[Any]:
    """A connection of its own, outside the pool, with no client or server statement
    timeout: for work that runs as long as it takes (migrations, an erasure's deletes).
    A CLI process's first one checks the deployment (`expect_deployment`)."""
    import asyncpg

    _require_dsn()
    try:
        conn = await asyncpg.connect(
            settings.pg_dsn,
            command_timeout=None,
            server_settings={
                "application_name": "loom",
                "synchronous_commit": "on",
                "default_transaction_isolation": "read committed",
                "statement_timeout": "0",
                # Its statements may run long; a transaction left idle by a stalled
                # process does not hold its locks past the pool's bound.
                "idle_in_transaction_session_timeout": idle_timeout_ms(),
            },
        )
    except Exception as e:
        _raise_if_unavailable(e)
        raise
    # Opening includes the deployment check and the codecs (jsonb reads back as a dict, as
    # the pool's): an outage there is the 503; the block's own errors pass through as
    # they are (an erasure's deadlock reaches its retry).
    try:
        await _check_deployment(conn)
        await init_connection(conn)
    except BaseException as e:
        conn.terminate()  # never handed out: nothing to finish, and it cannot raise
        if isinstance(e, Exception):
            _raise_if_unavailable(e)
        raise
    try:
        yield conn
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
                await _rollback(conn, tx)
                if isinstance(e, Exception):
                    _raise_if_unavailable(e)
                raise
            # The commit waits for the synchronous standby, outside the statement timeout;
            # the client's command timeout cancels the wait, which leaves the commit on the
            # primary alone, and raises outcome_unknown.
            committing = True
            try:
                await tx.commit()
            except Exception as e:
                _raise_if_unavailable(e, outcome_unknown=True)
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
            _raise_if_unavailable(e)
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
    import asyncpg

    try:
        if conn.is_closed():
            return
    except asyncpg.InterfaceError:
        return  # lost: the driver detached it from the pool (`_terminate`)
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


async def bump_policy(org: str, scope: str = "*") -> None:
    """Advance a policy generation (org '' for the deployment's): an admission that
    reached its verdict under the old policy starts over (`pg_memory.commit_versions`).
    Fails closed: the change answers 503 until the generation moves, and its retry
    moves it."""
    if not enabled():
        return
    async with transaction() as conn:
        # After every commit that holds the policy shared (`pg_memory.commit_versions`).
        await conn.execute(
            f"SELECT pg_advisory_xact_lock({POLICY_LOCK_KEY}) "
            "FROM (SELECT $1::text AS org, $2::text AS scope) AS k",
            org,
            scope,
        )
        await conn.execute(
            "INSERT INTO loom.scope_generations (org, scope, generation) VALUES ($1, $2, 1) "
            "ON CONFLICT (org, scope) "
            "DO UPDATE SET generation = loom.scope_generations.generation + 1",
            org,
            scope,
        )
