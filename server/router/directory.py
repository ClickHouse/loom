"""The router's directory, in its own Postgres database (`LOOM_ROUTER_PG_DSN`):
`loom_router.cells`, `placements`, `hosts` and `audit` (`schemas/router_migrations/`,
docs/architecture/cells.md, "The router").

Every statement the router runs against it is here. Three things serialize placements:

- the placing cell's row lock (`FOR UPDATE`), which a live placement and an attach batch
  (`attach_place`) both take before they insert, so a cell's count and quota are judged one
  placement at a time; a live placement reads the winner again once it holds the lock, so an
  org a batch placed meanwhile is answered as placed;
- transaction-scoped org locks (`_PLACE_LOCK`) before cell locks for live placements and
  configuration updates. Configuration takes distinct numeric hash keys in order; the
  first attach batch needs none while no other cell exists;
- the placements' primary key, as a backstop. Live placement and first attach use
  `ON CONFLICT DO NOTHING`; configuration inserts after its locked winner read. Only
  inserted rows are counted. Every successful call writes an audit row before committing,
  so a duplicate winner waits for synchronous durability too.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from server.core import cell as cell_state
from server.core import pg
from server.core.paths import ROUTER_MIGRATIONS_DIR
from server.router import fingerprints
from server.settings import settings

# The directory's own migration lock: never the memory authority's.
_LOCK_KEY = 0x6C6F6F6D_726F7574  # "loomrout"
# The first key of the per-org placement lock (`pg_advisory_xact_lock(int, int)`).
_PLACE_LOCK = 0x6C726F70  # "lrop"

_pool: Any = None
_opening = asyncio.Lock()

# Connections one router replica holds: its statements are short and few at a time.
POOL_MAX = 10  # not a setting: a replica's placement rate bounds what it needs
# Of those, the most that lookups, which anyone may send, hold at once: a lookup flood
# leaves the placements their connections.
LOOKUP_SLOTS = POOL_MAX // 2
_lookups = asyncio.Semaphore(LOOKUP_SLOTS)
# Seconds a statement, or the wait for a connection (the pool's opening included), may
# take before the call answers 503; statements are bounded on the server too, and a
# transaction left idle is ended at twice that: a stalled router never keeps a cell's row
# lock. A cell's placement call gives up at its own LOOM_CELL_ROUTER_TIMEOUT_S.
STATEMENT_TIMEOUT_S = 10  # not a setting: the cell's own timeout bounds it


def _bound_s(statements: int) -> int:
    """The longest a directory call of `statements` statements can take: the connection
    wait, then each statement and the pool's reset of the connection as it is released
    (asyncpg's), at the client bound each."""
    return STATEMENT_TIMEOUT_S + (statements + 1) * (STATEMENT_TIMEOUT_S + 1)


# New placement: BEGIN, org lock, winner, cell lock, winner, insert, counter, winner,
# audit, COMMIT. The audit also gives duplicate winners a synchronous durability fence.
PLACE_STATEMENTS = 10
PLACE_MAX_S = _bound_s(PLACE_STATEMENTS)  # not a setting: derived
# The longest limit/credential change has ten statements: begin, directory lock, row,
# other row, WAL, credential check, update, audit, read-back and commit.
OPERATOR_CHANGE_MAX_S = _bound_s(10)  # not a setting: derived from the bound above
# The longest a registration's directory write can take: thirteen statements (begin, lock,
# row, five refusal reads, three inserts, read-back, commit); and its read before the probe:
# six statements (the row, the first cell, then the credential, deployment id, host and
# bucket reads). tests/unit/test_cell_register.py counts the statements against the code.
REGISTER_STATEMENTS, REFUSAL_READ_STATEMENTS = 13, 6
REGISTER_MAX_S = _bound_s(REGISTER_STATEMENTS)
REFUSAL_READ_MAX_S = _bound_s(REFUSAL_READ_STATEMENTS)
# The attach calls' statements, which tests/unit/test_router_attach.py counts against the
# code: the operator's read of a cell's row (the row, any other row, audit); the read before
# `begin`'s and `serving`'s probe (the row, any other row); `begin`, `enforced` and
# `serving` (begin, lock, row, other row, WAL, update, audit, read-back, commit); a batch
# (begin, lock, row, other row, WAL, insert, placements elsewhere, count, audit, commit);
# removal adds other-row and WAL reads before deleting hosts/row and auditing.
OPERATOR_READ_STATEMENTS, ATTACH_READ_STATEMENTS = 3, 2
ATTACH_CHANGE_STATEMENTS, ATTACH_PLACE_STATEMENTS, REMOVE_STATEMENTS = 9, 10, 9
OPERATOR_READ_MAX_S = _bound_s(OPERATOR_READ_STATEMENTS)  # not a setting: derived
REMOVE_MAX_S = _bound_s(REMOVE_STATEMENTS)  # not a setting: derived
# Every directory connection commits through the synchronous standby and reads what
# committed before each statement, whatever the database's defaults: a placement the
# router answered survives a failover, and a later placement of one org sees the earlier.
_SESSION = {
    "application_name": "loom-router",
    "synchronous_commit": "on",
    "default_transaction_isolation": "read committed",
}


@dataclass(frozen=True)
class Cell:
    """What the router's APIs read of a cell's row."""

    cell: str
    is_first: bool
    host: str
    state: str
    mode: str
    closed: bool
    placements_max: int | None
    placed: int
    credential_sha256: str
    deployment_id: str
    current_fp: dict[str, Any] | None
    next_fp: dict[str, Any] | None
    # The new credential's hash during a rotation (`loom cell credential --next`).
    credential_next_sha256: str | None
    mutation_generation: UUID

    def holds(self, presented: str) -> bool:
        """Whether `presented` (a credential's SHA-256) is this cell's, current or next."""
        hashes = (self.credential_sha256, self.credential_next_sha256)
        return holds(presented, [h for h in hashes if h])


def holds(presented: str, hashes: Iterable[str]) -> bool:
    """Whether `presented` (a credential's SHA-256) is one of `hashes` (each set), in
    constant time each."""
    return any(hmac.compare_digest(presented, h) for h in hashes)


class Refusal(RuntimeError):
    """An answer the router gives in words it wrote: `reason`, never a driver's text
    (`_reading` names only the error's type), is what a caller is told."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DirectoryUnavailable(Refusal):
    """The directory's Postgres could not be read or written."""


class PlacementRefused(Refusal):
    """The cell may not place a new org now; `reason` says why."""


class OrgTooLong(Refusal):
    """A new org name longer than `cell.ORG_MAX_BYTES`: refused before its placement is
    written, by every route that places, and no retry places it."""


def _require_dsn() -> str:
    if not settings.router_pg_dsn:
        raise DirectoryUnavailable("no directory: set LOOM_ROUTER_PG_DSN")
    return settings.router_pg_dsn


class _PoolInitFailure(Exception):
    """Carry native control intent through the driver's ordinary-error cleanup."""

    def __init__(self, error: SystemExit | KeyboardInterrupt) -> None:
        super().__init__("directory codec initialization interrupted")
        self.error = error


async def _init(conn: Any) -> None:
    try:
        for typ in ("json", "jsonb"):
            await conn.set_type_codec(
                typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )
    except BaseException as original:
        # Only builtin codecs run here: this raw session has never been borrowed or
        # held command locks. Quarantine before the driver can lose its ownership.
        try:
            conn.terminate()
        except BaseException:  # noqa: BLE001 — disposal must preserve the first init failure.
            try:
                original.add_note("directory initialization session closure is unknown")
            except BaseException:  # noqa: BLE001 — diagnostics must preserve the first failure.
                pass
        if isinstance(original, (SystemExit, KeyboardInterrupt)):
            raise _PoolInitFailure(original) from None
        raise


async def pool() -> Any:
    """The directory's pool; physical sessions open only for foreground consumers."""
    global _pool
    if _pool is not None:
        return _pool
    import asyncpg

    async with _opening:
        if _pool is None:
            try:
                _pool = await asyncpg.create_pool(
                    _require_dsn(),
                    min_size=0,
                    max_size=POOL_MAX,
                    init=_init,
                    server_settings={
                        **_SESSION,
                        "statement_timeout": str(STATEMENT_TIMEOUT_S * 1000),
                        "idle_in_transaction_session_timeout": str(STATEMENT_TIMEOUT_S * 2000),
                    },
                    command_timeout=STATEMENT_TIMEOUT_S + 1,
                    timeout=STATEMENT_TIMEOUT_S,
                )
            except _PoolInitFailure as failed:
                raise failed.error from None
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def _connection(*, lookup: bool = False) -> AsyncIterator[Any]:
    """A pool connection, waited for at most STATEMENT_TIMEOUT_S in all (a lookup's
    slot, the pool's opening and the connection); a lookup's also takes one of
    LOOKUP_SLOTS. A wait past the bound raises TimeoutError, an OSError, which `_reading`
    answers as the directory unavailable."""
    try:
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(STATEMENT_TIMEOUT_S):
                if lookup:
                    await _lookups.acquire()
                    stack.callback(_lookups.release)
                conn = await stack.enter_async_context((await pool()).acquire())
            yield conn
    except _PoolInitFailure as failed:
        # Driver acquisition and context cleanup finish before control intent returns.
        raise failed.error from None


@asynccontextmanager
async def _dedicated() -> AsyncIterator[Any]:
    import asyncpg

    conn = await asyncpg.connect(_require_dsn(), command_timeout=None, server_settings=_SESSION)
    try:
        yield conn
    finally:
        await conn.close()


async def migrate() -> list[str]:
    """Apply the directory's pending migrations; their names. A database that holds a
    memory authority's ledger is refused: the directory is never a cell's database."""
    from server.core.schema import pg_migrate

    async with _reading():
        async with _dedicated() as conn:
            if await conn.fetchval("SELECT to_regclass('loom.pg_migrations') IS NOT NULL"):
                raise DirectoryUnavailable(
                    "this database holds a memory authority (loom.pg_migrations): "
                    "give the directory a database of its own"
                )
        return await pg_migrate.migrate(
            ROUTER_MIGRATIONS_DIR, connect=_dedicated, schema="loom_router", lock_key=_LOCK_KEY
        )


# The columns a Cell holds (read by name: `_cell`).
_CELL_COLUMNS = (
    "cell, is_first, host, state, mode, closed, placements_max, placed, credential_sha256, "
    "credential_next_sha256, deployment_id, current_fp, next_fp, mutation_generation"
)
_CELL = f"SELECT {_CELL_COLUMNS} FROM loom_router.cells"


# The row as registration reads it again: the Cell's columns and what a rerun compares
# (its bucket, prefix and addresses, which never move on a rerun).
_REGISTERED = ("bucket", "bucket_prefix", "probe_url", "upstream_url")
_CELL_REGISTERED = f"SELECT {_CELL_COLUMNS}, {', '.join(_REGISTERED)} FROM loom_router.cells"
_ROUTING = "SELECT cell, host, state, is_first, issuers, upstream_url FROM loom_router.cells"


_WINNER = (
    "SELECT p.cell, c.deployment_id FROM loom_router.placements AS p "
    "JOIN loom_router.cells AS c USING (cell) WHERE p.org = $1"
)


def _cell(row: Any) -> Cell:
    return Cell(**dict(row))


async def routing() -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """What routing reads (`server.router.routing.build`): every cell's name, host,
    state, first mark, issuers and upstream address, and every host alias."""
    async with _reading(), _connection() as conn:
        cells = await conn.fetch(f"{_ROUTING} ORDER BY cell")
        aliases = await conn.fetch("SELECT host, cell FROM loom_router.hosts")
    return [dict(r) for r in cells], [(str(r["host"]), str(r["cell"])) for r in aliases]


async def credentials() -> dict[str, tuple[str, ...]]:
    """Every cell's credential hashes, current then next (during a rotation), by cell."""
    async with _reading(), _connection() as conn:
        rows = await conn.fetch(
            "SELECT cell, credential_sha256, credential_next_sha256 FROM loom_router.cells"
        )
    return {
        str(r["cell"]): tuple(
            str(h) for h in (r["credential_sha256"], r["credential_next_sha256"]) if h
        )
        for r in rows
    }


class OperatorRefused(Refusal):
    """An operator change the directory refuses as it stands; `reason` says why."""


class NoSuchCell(OperatorRefused):
    """The directory has no row for the cell named."""


# The directory lock, apart from the placements' per-org one. It keeps the rule the unique
# indexes cannot hold across two columns: a hash names one cell, as current or next. Any
# writer of credential_sha256 or credential_next_sha256 takes it and checks both columns.
_DIRECTORY_LOCK = 0x6C726F64  # "lrod"


class _Unset:
    """A `set_limit` field not given: not written (None is a value, a quota cleared)."""


UNSET = _Unset()


async def set_limit(
    cell_name: str,
    *,
    closed: bool | _Unset = UNSET,
    placements_max: int | None | _Unset = UNSET,
    expected: Control,
) -> Cell:
    """`loom cell limit`: close or reopen a cell to new orgs and set or clear its
    placement quota, on its row alone, audited; the cell's stores are not touched. A
    field left UNSET is not written (a quota of None clears it)."""

    if closed is UNSET and placements_max is UNSET:
        raise ValueError("set_limit sets closed, placements_max or both")

    async def change(conn: Any, row: Cell) -> dict[str, Any]:
        if closed is False and row.state == "attaching":
            raise OperatorRefused(
                f"cell {cell_name!r} is attaching: it can reopen after attach reaches serving"
            )
        detail: dict[str, Any] = {}
        if closed is not UNSET:
            detail["closed"] = closed
        if placements_max is not UNSET:
            detail["placements_max"] = placements_max
        sets = ", ".join(f"{column} = ${i}" for i, column in enumerate(detail, start=2))
        await conn.execute(
            f"UPDATE loom_router.cells SET {sets}, updated_at = now() WHERE cell = $1",
            cell_name,
            *detail.values(),
        )
        return detail

    return await _operator_change(cell_name, "limit", change, expected)


async def rotate_credential(
    cell_name: str, step: str, sha256: str = "", *, expected: Control
) -> Cell:
    """`loom cell credential`: `next` records a new credential's hash beside the
    current one, refused when any cell holds it as current or next and while one is
    pending; `finish` makes `next` current and drops the old; `revert` drops `next`.
    `finish` and `revert` refuse with no `next` pending."""
    if step not in ("next", "finish", "revert"):
        raise ValueError(f"no credential step {step!r}")
    if (step == "next") != bool(sha256):
        raise ValueError("a sha256 goes with the next step alone")

    async def change(conn: Any, row: Cell) -> dict[str, Any]:
        if step == "next":
            if row.credential_next_sha256 == sha256:
                raise OperatorRefused(f"that hash is already cell {cell_name!r}'s next credential")
            if row.credential_next_sha256:
                raise OperatorRefused(f"cell {cell_name!r} has another next credential pending")
            taken = await conn.fetchval(
                "SELECT cell FROM loom_router.cells "
                "WHERE credential_sha256 = $1 OR credential_next_sha256 = $1",
                sha256,
            )
            if taken:
                raise OperatorRefused(f"cell {taken!r} records that credential hash already")
            sql = "SET credential_next_sha256 = $2"
            args: tuple[Any, ...] = (cell_name, sha256)
        elif not row.credential_next_sha256:
            raise OperatorRefused(f"cell {cell_name!r} has no next credential pending")
        elif step == "finish":
            sql = "SET credential_sha256 = credential_next_sha256, credential_next_sha256 = NULL"
            args = (cell_name,)
        else:  # revert
            sql = "SET credential_next_sha256 = NULL"
            args = (cell_name,)
        await conn.execute(
            f"UPDATE loom_router.cells {sql}, updated_at = now() WHERE cell = $1", *args
        )
        return {"step": step, **({"sha256": sha256} if sha256 else {})}

    return await _operator_change(cell_name, "credential", change, expected)


async def _operator_change(
    cell_name: str,
    action: str,
    change: Callable[[Any, Cell], Awaitable[dict[str, Any]]],
    expected: Control,
) -> Cell:
    async def change_locked(conn: Any, found: Attaching) -> Cell:
        detail = await change(conn, found.row)
        await _audit(conn, action, {"cell": cell_name, **detail})
        row = await conn.fetchrow(f"{_CELL} WHERE cell = $1", cell_name)
        return _cell(row)

    return await _attach_change(cell_name, expected, change_locked)


@dataclass(frozen=True)
class Registration:
    """What `loom cell prepare` registers: the cell's host (as `normalise_host` writes
    it), the deployment id its stores record, the SHA-256 of the credential it calls the
    router with, its document bucket and prefix, and the address its probe listener
    answers at (the router's own LOOM_ROUTER_UPSTREAM_PROBE_URL for the first cell)."""

    cell: str
    host: str
    deployment_id: str
    credential_sha256: str
    bucket: str
    bucket_prefix: str
    probe_url: str
    is_first: bool
    # What prepare counted in the cell's stores and processes (_held_refusal judges them).
    tenancy_rows: int
    state_rows: int
    prefix_objects: int
    processes_without_cell: int
    # Where the router forwards to the cell: the first cell's is the deployment the
    # router was put in front of (LOOM_ROUTER_UPSTREAM_URL), so registering it changes no
    # route; a later cell's is '' until attach writes it.
    upstream_url: str = ""


def _overlaps(prefix: str, other: str) -> bool:
    """Whether two prefixes in one bucket share objects: one is the other or lies under
    it, by path segment (`a` holds `a/b`, not `ab`); the empty prefix holds them all."""
    a, b = prefix.strip("/"), other.strip("/")
    return not a or not b or a == b or a.startswith(b + "/") or b.startswith(a + "/")


async def register(reg: Registration) -> tuple[Cell, bool]:
    """`loom cell prepare`: the cell's row as `attaching` (mode `recording` on the first
    cell, `enforced` on a later one), its host row and an audit row, in one transaction
    under the directory lock; and whether this call wrote them.

    A rerun (a row of the same name) answers that row, or is refused when its first-cell
    standing, deployment id, host, bucket, prefix or addresses differ from the row's or its
    hash is neither the row's current nor next (`_rerun_refusal`); it runs none of the
    checks below.

    Refused: stores holding tenancy or state rows, a later cell whose bucket prefix holds
    objects or whose processes run without its LOOM_CELL, a second first cell, a later cell
    before the first is `serving`, a first cell with no upstream to copy, a credential hash
    any cell holds as current or next, a deployment id or host another cell holds, and a
    bucket prefix that shares objects with another cell's in a bucket of the same name."""
    async with _reading(), _connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _DIRECTORY_LOCK)
        held = await conn.fetchrow(f"{_CELL_REGISTERED} WHERE cell = $1 FOR UPDATE", reg.cell)
        if held is not None:
            row = _cell({k: v for k, v in dict(held).items() if k not in _REGISTERED})
            if refusal := _rerun_refusal(held, reg):
                raise OperatorRefused(refusal)
            return row, False
        await _refuse_registration(conn, reg)
        await conn.execute(
            "INSERT INTO loom_router.cells (cell, is_first, host, state, mode, mode_set_at, "
            "credential_sha256, deployment_id, bucket, bucket_prefix, probe_url, upstream_url) "
            "VALUES ($1, $2, $3, 'attaching', $4, now(), $5, $6, $7, $8, $9, $10)",
            reg.cell,
            reg.is_first,
            reg.host,
            "recording" if reg.is_first else "enforced",
            reg.credential_sha256,
            reg.deployment_id,
            reg.bucket,
            reg.bucket_prefix,
            reg.probe_url,
            reg.upstream_url,
        )
        await conn.execute(
            "INSERT INTO loom_router.hosts (host, cell) VALUES ($1, $2)", reg.host, reg.cell
        )
        await conn.execute(
            "INSERT INTO loom_router.audit (actor, action, detail) VALUES ('operator', $1, $2)",
            "register",
            {
                "cell": reg.cell,
                "host": reg.host,
                "deployment_id": reg.deployment_id,
                "is_first": reg.is_first,
                "bucket": reg.bucket,
                "bucket_prefix": reg.bucket_prefix,
                "probe_url": reg.probe_url,
                "upstream_url": reg.upstream_url,
            },
        )
        written = await conn.fetchrow(f"{_CELL} WHERE cell = $1", reg.cell)
    return _cell(written), True


def _rerun_refusal(held: Mapping[str, Any], reg: Registration) -> str:
    """Why a rerun of `reg` against its row (`_CELL_REGISTERED`) is refused, '' when it is
    not. A rerun never moves an address: the row keeps what registration wrote, and a
    router replica configured with others (a rollout not finished) is refused."""
    cell = reg.cell
    if bool(held["is_first"]) != reg.is_first:
        if reg.is_first:
            return f"the shared host names cell {cell!r} as the first, and the directory does not"
        return (
            f"cell {cell!r} is the directory's first cell, and its host is no longer the shared "
            "host"
        )
    if (held["deployment_id"], held["host"]) != (reg.deployment_id, reg.host):
        return f"cell {cell!r} is registered with another deployment id or host"
    hashes = [h for h in (held["credential_sha256"], held["credential_next_sha256"]) if h]
    if not holds(reg.credential_sha256, hashes):
        return f"that credential hash is not cell {cell!r}'s: rotate it with `loom cell credential`"
    if (held["bucket"], held["bucket_prefix"]) != (reg.bucket, reg.bucket_prefix):
        return (
            f"cell {cell!r} is registered with another bucket or prefix, which never change "
            "after prepare"
        )
    if held["probe_url"] != reg.probe_url:
        if reg.is_first:
            return (
                f"cell {cell!r} is registered with probe address {held['probe_url']}: set this "
                "router replica's LOOM_ROUTER_UPSTREAM_PROBE_URL to it"
            )
        return (
            f"cell {cell!r} is registered with probe address {held['probe_url']}: run "
            "`loom cell prepare` with that --probe-url"
        )
    if reg.is_first and reg.upstream_url and held["upstream_url"] != reg.upstream_url:
        return (
            f"cell {cell!r} is registered with upstream {held['upstream_url']}: set this router "
            "replica's LOOM_ROUTER_UPSTREAM_URL to it"
        )
    return ""


def _order_refusal(reg: Registration, first: Mapping[str, Any] | None) -> str:
    """Why a new cell cannot register given the first cell's name and state (`first`, None
    before one is), '' when it can: one first cell, registered before any other and
    `serving` before another registers (so no later cell appears mid-attach), with the
    router's upstream to copy."""
    if reg.is_first and first is not None:
        return f"cell {first['cell']!r} is the deployment's first cell already"
    if not reg.is_first and first is None:
        return "the first cell is registered before any other"
    if not reg.is_first and first is not None and first["state"] != "serving":
        return (
            f"the first cell {first['cell']!r} is not serving yet: a later cell registers "
            "once the first cell's attach is done"
        )
    if reg.is_first and not reg.upstream_url:
        return (
            "the router needs LOOM_ROUTER_UPSTREAM_URL (http or https with a host) to register "
            "the first cell"
        )
    return ""


async def refusal_before_probe(reg: Registration) -> str:
    """Why `register` will refuse `reg` on what it can know before the probe ('' when
    nothing yet): a rerun's checks, or every refusal of a new cell. Read first so a refusal is
    named at once rather than probed (and perhaps retried as unreachable); `register`
    answers the same under its lock."""
    async with _reading(), _connection() as conn:
        held = await conn.fetchrow(f"{_CELL_REGISTERED} WHERE cell = $1", reg.cell)
        if held is not None:
            return _rerun_refusal(held, reg)
        # A new cell: every refusal registration would give, read here without the lock.
        try:
            await _refuse_registration(conn, reg)
        except OperatorRefused as e:
            return e.reason
    return ""


def _held_refusal(reg: Registration) -> str:
    """Why a new cell's stores and processes, as prepare counted them, cannot register
    ('' when they can)."""
    if reg.tenancy_rows:
        return (
            "the cell's Postgres holds tenancy rows while the directory has no row for it: "
            "its stores carry another cell's orgs"
        )
    if reg.state_rows:
        return (
            "the cell's Postgres holds a `first`, mode or own row while the directory has no "
            "row for it: its stores carry another cell's state"
        )
    if reg.prefix_objects and not reg.is_first:
        return (
            "a later cell's bucket prefix must hold no object but prepare's probe, and it "
            "holds others: another deployment's documents"
        )
    if reg.processes_without_cell and not reg.is_first:
        return (
            "live processes of a later cell run without its LOOM_CELL: a later cell runs with "
            "it from its first boot"
        )
    return ""


async def _refuse_registration(conn: Any, reg: Registration) -> None:
    first = await conn.fetchrow("SELECT cell, state FROM loom_router.cells WHERE is_first")
    if refusal := _held_refusal(reg) or _order_refusal(reg, first):
        raise OperatorRefused(refusal)
    taken = await conn.fetchval(
        "SELECT cell FROM loom_router.cells "
        "WHERE credential_sha256 = $1 OR credential_next_sha256 = $1",
        reg.credential_sha256,
    )
    if taken:
        raise OperatorRefused(f"cell {taken!r} records that credential hash already")
    taken = await conn.fetchval(
        "SELECT cell FROM loom_router.cells WHERE deployment_id = $1", reg.deployment_id
    )
    if taken:
        raise OperatorRefused(f"cell {taken!r} records deployment id {reg.deployment_id!r}")
    taken = await conn.fetchval(
        "SELECT cell FROM loom_router.cells WHERE host = $1 "
        "UNION ALL SELECT cell FROM loom_router.hosts WHERE host = $1 LIMIT 1",
        reg.host,
    )
    if taken:
        raise OperatorRefused(f"host {reg.host!r} names cell {taken!r} already")
    if reg.bucket:
        others = await conn.fetch(
            "SELECT cell, bucket_prefix FROM loom_router.cells WHERE bucket = $1", reg.bucket
        )
        for other in others:
            if _overlaps(reg.bucket_prefix, str(other["bucket_prefix"])):
                raise OperatorRefused(
                    f"bucket {reg.bucket!r} prefix {reg.bucket_prefix!r} shares objects "
                    f"with cell {other['cell']!r}'s ({other['bucket_prefix']!r})"
                )


async def first_cell() -> Cell | None:
    """The first cell's row, for a lookup of an org with no placement."""
    async with _reading(), _connection(lookup=True) as conn:
        row = await conn.fetchrow(f"{_CELL} WHERE is_first")
    return _cell(row) if row else None


async def first_cell_named(cell_name: str) -> Cell | None:
    """The first cell's row when it is `cell_name`, else None: the state route's read
    (`Router._state_row`), at most once per replica per window. Not a lookup: it takes no
    lookup slot."""
    async with _reading(), _connection() as conn:
        row = await conn.fetchrow(f"{_CELL} WHERE is_first AND cell = $1", cell_name)
    return _cell(row) if row else None


async def place(
    org: str,
    cell_name: str,
    refusal: Callable[[Cell], str],
    *,
    actor: str,
    verify: Callable[[Cell], str] | None = None,
) -> tuple[str, str]:
    """The permanent winner of `org`, with every successful call audited and committed
    synchronously. `verify` judges the requested cell's locked identity even for a winner
    already there; `refusal` and quota judge new placements only. A winner elsewhere needs
    neither check. The org lock and cell row lock serialize live and attach placements."""
    async with _reading(), _connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))", _PLACE_LOCK, org)
        row = await conn.fetchrow(_WINNER, org)
        if row is None or (verify is not None and row["cell"] == cell_name):
            if row is None:
                _refuse_long([org])
            locked = await conn.fetchrow(f"{_CELL} WHERE cell = $1 FOR UPDATE", cell_name)
            if locked is None:
                raise PlacementRefused(f"cell {cell_name!r} is not in the directory")
            candidate = _cell(locked)
            # An attach batch holds this row's lock rather than the org's.
            row = await conn.fetchrow(_WINNER, org)
            if (row is None or row["cell"] == cell_name) and verify is not None:
                if reason := verify(candidate):
                    raise PlacementRefused(reason)
            if row is None:
                await _place_one(conn, org, cell_name, candidate, refusal)
                row = await conn.fetchrow(_WINNER, org)
        winner = str(row["cell"]), str(row["deployment_id"])
        await conn.execute(
            "INSERT INTO loom_router.audit (actor, action, detail) VALUES ($1, 'place', $2)",
            actor,
            {"org": org, "cell": winner[0]},
        )
    return winner


async def _place_one(
    conn: Any, org: str, cell_name: str, candidate: Cell, refusal: Callable[[Cell], str]
) -> None:
    """Judge the locked row and insert the placement, counted only when written."""
    if reason := refusal(candidate):
        raise PlacementRefused(reason)
    if reason := _quota_refusal(candidate, 1):
        raise PlacementRefused(reason)
    inserted = await conn.fetchval(
        "INSERT INTO loom_router.placements (org, cell) VALUES ($1, $2) "
        "ON CONFLICT DO NOTHING RETURNING org",
        org,
        cell_name,
    )
    if inserted is None:
        return
    await conn.execute(
        "UPDATE loom_router.cells SET placed = placed + 1 WHERE cell = $1", cell_name
    )


def _quota_refusal(row: Cell, additional: int) -> str:
    if row.placements_max is not None and row.placed + additional > row.placements_max:
        return f"cell {row.cell!r} holds its quota of orgs"
    return ""


def _refuse_long(orgs: Iterable[str]) -> None:
    """OrgTooLong for the first org longer than `cell.ORG_MAX_BYTES`: the one check every
    route that inserts a placement runs, so no route places such an org."""
    for org in orgs:
        if reason := cell_state.org_name_refusal(org):
            raise OrgTooLong(reason)


# The first cell's attach (docs/architecture/cells.md, "The router"). Every change runs
# under the directory lock, then the cell's row lock, the order `register`,
# `_operator_change` and `remove` take, and refuses while another row exists: a later cell
# registered before the first attach could share the first cell's stores.

# The columns attach reads beside a Cell's.
_ATTACH_EXTRA = (*_REGISTERED, "attach_started_at")
_CELL_ATTACH = f"SELECT {_CELL_COLUMNS}, {', '.join(_ATTACH_EXTRA)} FROM loom_router.cells"
_OTHER = "SELECT cell FROM loom_router.cells WHERE cell <> $1 ORDER BY cell LIMIT 1"


@dataclass(frozen=True)
class Attaching:
    """A cell's row as attach reads it: the Cell, the stores and addresses its probe and
    upstream check compare, when attach began, and the name of any other row."""

    row: Cell
    bucket: str
    bucket_prefix: str
    probe_url: str
    upstream_url: str
    attach_started_at: datetime | None
    other: str | None

    @property
    def deployment_id(self) -> str:
        return self.row.deployment_id

    def served(self, deployment_id: str) -> bool:
        """Whether this is the first cell, `serving` already, with that deployment id:
        `attach/serving` is done."""
        row = self.row
        return row.is_first and row.state == "serving" and row.deployment_id == deployment_id


def _attaching(found: Any, other: str | None) -> Attaching:
    values = dict(found)
    extra = {k: values.pop(k) for k in _ATTACH_EXTRA}
    return Attaching(row=_cell(values), other=other, **extra)


@dataclass(frozen=True)
class Begin:
    """What `attach/begin` posts: the deployment id the cell's stores record, its
    fingerprints exactly as `loom.cell_self` stores them, its root-secret fingerprint and
    the allow lists attach judged the stores by (audited, never stored)."""

    deployment_id: str
    current_fp: dict[str, Any]
    next_fp: dict[str, Any] | None
    keys_fp: str
    allowed: dict[str, list[str]]


def attach_refusal(
    found: Attaching, deployment_id: str, *, begun: bool = True, enforced: bool = False
) -> str:
    """Why an attach call on this row is refused ('' when it is not): only the first cell,
    while `attaching`, called with the deployment id the row records, with no other row,
    (`begun`) once `attach/begin` has written `attach_started_at` and the fingerprints, and
    (`enforced`) once `attach/enforced` has recorded the directory mode."""
    row = found.row
    if not row.is_first:
        return f"cell {row.cell!r} is a later cell: the router attaches the first cell only"
    if row.state != "attaching":
        return f"cell {row.cell!r} is {row.state} already"
    if deployment_id != row.deployment_id:
        return (
            f"cell {row.cell!r} is registered with deployment id {row.deployment_id!r}, "
            f"not {deployment_id!r}"
        )
    if found.other is not None:
        return (
            f"cell {found.other!r} is registered while the first cell attaches: remove it "
            f"first (`loom cell remove --cell {found.other}`)"
        )
    if begun and (found.attach_started_at is None or row.current_fp is None):
        return f"the attach of cell {row.cell!r} has not begun: post attach/begin first"
    if enforced and row.mode != "enforced":
        return f"cell {row.cell!r} records mode {row.mode!r}: post attach/enforced first"
    return ""


def _as_json(*values: Any) -> str:
    """Values in one canonical JSON text, which tells `false`, `0` and `0.0` apart."""
    return json.dumps(values, sort_keys=True)


def begin_refusal(found: Attaching, begin: Begin) -> str:
    """Why `attach/begin` is refused on this row ('' when it is not), before the probe and
    again under the lock."""
    if reason := attach_refusal(found, begin.deployment_id, begun=False):
        return reason
    row = found.row
    registered = {
        "cell": row.cell,
        "deployment_id": row.deployment_id,
        "bucket": found.bucket,
        "bucket_prefix": found.bucket_prefix,
        "host": row.host,
    }
    if reason := fingerprints.registration_pair_refusal(
        registered, begin.current_fp, begin.next_fp
    ):
        return reason
    # Compared as JSON, not as Python values, where `false == 0` and `1 == 1.0`: a rerun
    # writes the fingerprints again only when they are the row's to the type.
    if row.current_fp is not None and _as_json(row.current_fp, row.next_fp) != _as_json(
        begin.current_fp, begin.next_fp
    ):
        return f"cell {row.cell!r} records other fingerprints: change them with `loom cell update`"
    return ""


async def attach_read(cell_name: str, *, audited: bool = False) -> Attaching:
    """The cell's row as attach reads it, and any other row's name (`audited`: with an
    audit row, as `GET /cells/v1/operator/cells/<cell>` writes); NoSuchCell without one."""
    async with _reading(), _connection() as conn:
        found = await conn.fetchrow(f"{_CELL_ATTACH} WHERE cell = $1", cell_name)
        other = await conn.fetchval(_OTHER, cell_name)
        if audited:
            await _audit(conn, "cell_read", {"cell": cell_name, "found": found is not None})
    if found is None:
        raise NoSuchCell(f"cell {cell_name!r} is not in the directory")
    return _attaching(found, other)


# Every acceptance controller compares the full registration and exact JSON pair.
CONTROL_IDENTITY = ("cell", "is_first", "host", "deployment_id", *_REGISTERED)
CONTROL_FIELDS = (*CONTROL_IDENTITY, "current_fp", "next_fp")
CONTROL_MAX_S = _bound_s(10)  # not a setting: transaction, commit and pool release bound


@dataclass(frozen=True)
class Control:
    """The operator's observed registration and pair, with its claimed generation."""

    values: dict[str, Any]
    generation: UUID | None


def control_refusal(found: Attaching, expected: Control, *, claim: bool = False) -> str:
    if not claim and expected.generation != found.row.mutation_generation:
        return "cell control generation changed: stop and rerun the command with a fresh claim"
    actual = {
        name: getattr(found.row, name) if hasattr(found.row, name) else getattr(found, name)
        for name in CONTROL_FIELDS
    }
    if _as_json(actual) != _as_json(expected.values):
        return (
            "cell registration or fingerprints changed: stop and rerun from a fresh directory read"
        )
    return ""


@dataclass(frozen=True)
class _ControlFatal:
    error: SystemExit | KeyboardInterrupt


async def _control_result[T](work: Callable[[], Awaitable[T]]) -> T | _ControlFatal:
    try:
        return await work()
    except (SystemExit, KeyboardInterrupt) as error:
        return _ControlFatal(error)
    except _ControlInterrupted as error:
        return error.fatal


class _ControlOwner:
    """Own only this operator attempt's proxy, through close and pool release."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds
        self.work_end = self.end - min(1.0, seconds / 4)
        self.context = _connection()
        self.conn: Any = None
        self.entered = False
        self.closing = False
        self.close_task: asyncio.Task[Any] | None = None
        self.exit_task: asyncio.Task[Any] | None = None

    def start[T](self, work: Callable[[], Awaitable[T]]) -> asyncio.Task[T | _ControlFatal]:
        task = asyncio.create_task(_control_result(work))
        task.add_done_callback(pg._retrieve_task)
        return task

    async def wait[T](self, task: asyncio.Task[T | _ControlFatal], end: float) -> T:
        done, _ = await asyncio.wait({task}, timeout=max(0, end - time.monotonic()))
        if not done:
            raise TimeoutError("directory control deadline expired; outcome unknown")
        result = task.result()
        if isinstance(result, _ControlFatal):
            # Keep fatal exceptions inert in nested acquisition/cleanup children.
            raise _ControlInterrupted(result)
        return result

    async def close(self) -> None:
        if self.conn is None:
            return
        if self.close_task is None:
            self.close_task = self.start(lambda: pg.close_owned(self.conn, end=self.end))
        interrupted: asyncio.CancelledError | None = None
        try:
            while not self.close_task.done():
                remaining = self.end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("directory control deadline expired; outcome unknown")
                try:
                    await asyncio.wait({self.close_task}, timeout=remaining)
                except asyncio.CancelledError as error:
                    # Another signal cannot abandon the public cancellation in flight.
                    interrupted = interrupted or error
            result = self.close_task.result()
            if isinstance(result, _ControlFatal):
                raise _ControlInterrupted(result)
        except BaseException:
            try:
                pg._terminate(self.conn)
            except BaseException:  # noqa: BLE001 — abort must preserve the first cleanup failure.
                pass
            finally:
                if not self.close_task.done():
                    self.close_task.cancel()
            if interrupted is not None:
                raise interrupted from None
            raise
        if interrupted is not None:
            raise interrupted

    async def release(self) -> None:
        if self.entered:
            if self.exit_task is None:
                self.exit_task = self.start(lambda: self.context.__aexit__(None, None, None))
            try:
                await self.wait(self.exit_task, self.end)
            except BaseException:  # An interrupted pool exit must not continue without its owner.
                self.exit_task.cancel()
                raise

    async def work[T](self, change: Callable[[Any], Awaitable[T]]) -> T:
        self.conn = await self.context.__aenter__()
        self.entered = True
        if self.closing:
            # A cancellation-resistant acquire cannot hand a late proxy to a mutation.
            try:
                await self.close()
            finally:
                await self.release()
            raise TimeoutError("directory connection arrived after its attempt ended")
        tx = self.conn.transaction()
        await tx.start()
        result = await change(self.conn)
        await tx.commit()
        return result

    async def finish(self, task: asyncio.Task[Any]) -> None:
        self.closing = True
        failure: BaseException | None = None
        try:
            await self.close()
        except BaseException as error:  # noqa: BLE001 — release must preserve the first failure.
            failure = error
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait({task}, timeout=max(0, self.end - time.monotonic()))
        except BaseException as error:  # noqa: BLE001 — cleanup keeps the first failure.
            failure = failure or error
        try:
            await self.release()
        except BaseException as error:  # noqa: BLE001 — cleanup keeps the first failure.
            failure = failure or error
        if failure is not None:
            raise failure


class _ControlInterrupted(BaseException):
    def __init__(self, fatal: _ControlFatal) -> None:
        self.fatal = fatal


async def _control_transaction[T](change: Callable[[Any], Awaitable[T]], *, seconds: float) -> T:
    """Bound the whole callback, close first, then settle work and pool ownership."""
    async with _reading():
        owner = _ControlOwner(seconds)
        task = owner.start(lambda: owner.work(change))
        try:
            result = await owner.wait(task, owner.work_end)
        except BaseException as original:
            try:
                await owner.finish(task)
            except BaseException:  # noqa: BLE001 — preserve the original failure/cancellation.
                pass
            if isinstance(original, _ControlInterrupted):
                raise original.fatal.error from None
            raise
        try:
            await owner.finish(task)
        except _ControlInterrupted as interrupted:
            raise interrupted.fatal.error from None
        return result


async def _attach_change[T](
    cell_name: str,
    expected: Control,
    change: Callable[[Any, Attaching], Awaitable[T]],
    *,
    claim: bool = False,
) -> T:
    async def locked(conn: Any) -> T:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _DIRECTORY_LOCK)
        found = await conn.fetchrow(f"{_CELL_ATTACH} WHERE cell = $1 FOR UPDATE", cell_name)
        if found is None:
            raise NoSuchCell(f"cell {cell_name!r} is not in the directory")
        other = await conn.fetchval(_OTHER, cell_name)
        attaching = _attaching(found, other)
        if reason := control_refusal(attaching, expected, claim=claim):
            raise OperatorRefused(reason)
        # Even a no-op needs synchronous WAL acknowledgement.
        await conn.execute("SELECT pg_logical_emit_message(true, 'loom.control', '')")
        return await change(conn, attaching)

    return await _control_transaction(locked, seconds=CONTROL_MAX_S)


async def claim(cell_name: str, expected: Control) -> Attaching:
    """Rotate on EVERY execution, including an identical HTTP replay."""

    async def change_locked(conn: Any, _found: Attaching) -> Attaching:
        await conn.execute(
            "UPDATE loom_router.cells SET mutation_generation = gen_random_uuid() WHERE cell = $1",
            cell_name,
        )
        await _audit(conn, "control_claim", {"cell": cell_name})
        written = await conn.fetchrow(f"{_CELL_ATTACH} WHERE cell = $1", cell_name)
        return _attaching(written, _found.other)

    return await _attach_change(cell_name, expected, change_locked, claim=True)


async def update_addresses(
    cell_name: str,
    probe_url: str,
    upstream_url: str,
    *,
    expected: Control,
    configured: Callable[[], bool],
) -> Attaching:
    """Commit only the addresses whose stores were just proved outside this transaction."""

    async def change(conn: Any, found: Attaching) -> Attaching:
        if not configured():
            raise OperatorRefused(
                "the contacted router's configured addresses changed during proof"
            )
        if not found.row.is_first and upstream_url != found.upstream_url:
            raise OperatorRefused("a later cell probe refresh cannot change its upstream address")
        await conn.execute(
            (
                "UPDATE loom_router.cells SET probe_url=$2, upstream_url=$3, updated_at=now() "
                "WHERE cell=$1"
            ),
            cell_name,
            probe_url,
            upstream_url,
        )
        await _audit(
            conn,
            "addresses",
            {"cell": cell_name, "probe_url": probe_url, "upstream_url": upstream_url},
        )
        row = await conn.fetchrow(f"{_CELL_ATTACH} WHERE cell=$1", cell_name)
        return _attaching(row, found.other)

    return await _attach_change(cell_name, expected, change)


async def configuration_placements(
    cell_name: str, orgs: list[str], *, expected: Control, keys_fp: str
) -> list[str]:
    """Acknowledge actual immutable winners before the cell repairs local tenancy."""
    _refuse_long(orgs)

    async def read(conn: Any, found: Attaching) -> list[str]:
        if (
            await conn.fetchval("SELECT keys_fp FROM loom_router.cells WHERE cell=$1", cell_name)
            != keys_fp
        ):
            raise OperatorRefused(
                "cell root-secret fingerprint changed; configuration update refuses"
            )
        accepted = [
            value for value in (found.row.current_fp, found.row.next_fp) if value is not None
        ]
        if not accepted or any(not fingerprints.readable(value) for value in accepted):
            raise OperatorRefused(
                "configured placement proof needs readable directory reservations"
            )
        configured = {
            org for value in accepted for org in value["fields"].get("configured_orgs", [])
        }
        if not set(orgs) <= configured:
            raise OperatorRefused("placement proof names an org outside the reserved configuration")
        rows = await conn.fetch(
            "SELECT org, cell FROM loom_router.placements WHERE org=ANY($1::text[]) ORDER BY org",
            sorted(set(orgs)),
        )
        for row in rows:
            if row["cell"] != cell_name:
                raise OperatorRefused(f"org {row['org']!r} is placed in cell {row['cell']!r}")
        await _audit(
            conn,
            "configuration_placements",
            {"cell": cell_name, "named": len(set(orgs)), "held": len(rows)},
        )
        return [str(row["org"]) for row in rows]

    return await _attach_change(cell_name, expected, read)


async def update_configuration(
    cell_name: str,
    target: dict[str, Any],
    action: str,
    *,
    keys_fp: str,
    refusal: Callable[[Cell], str],
    expected: Control,
) -> Attaching:
    """Reserve acceptance and configured placements in one bounded directory commit."""
    try:
        current, pending = fingerprints.configuration_pair(
            expected.values.get("current_fp"), expected.values.get("next_fp"), target, action
        )
    except fingerprints.ConfigurationRefused as error:
        raise OperatorRefused(error.reason) from None
    accepted = [value for value in (current, pending) if value is not None]
    desired_orgs = {org for value in accepted for org in value["fields"].get("configured_orgs", [])}
    prior_orgs = {
        org
        for value in (expected.values["current_fp"], expected.values["next_fp"])
        if value is not None
        for org in value["fields"].get("configured_orgs", [])
    }
    orgs = sorted(desired_orgs | prior_orgs)
    _refuse_long(orgs)

    async def locked(conn: Any) -> Attaching:
        # Numeric hash order matches live placement locks, including hash collisions.
        await conn.execute(
            "WITH keys AS MATERIALIZED (SELECT DISTINCT hashtext(org) AS key "
            "FROM unnest($2::text[]) AS org ORDER BY key) "
            "SELECT pg_advisory_xact_lock($1, key) FROM keys ORDER BY key",
            _PLACE_LOCK,
            orgs,
        )
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _DIRECTORY_LOCK)
        raw = await conn.fetchrow(
            f"SELECT {_CELL_COLUMNS}, {', '.join(_ATTACH_EXTRA)}, keys_fp "
            "FROM loom_router.cells WHERE cell=$1 FOR UPDATE",
            cell_name,
        )
        if raw is None:
            raise NoSuchCell(f"cell {cell_name!r} is not in the directory")
        row = dict(raw)
        recorded_keys = row.pop("keys_fp")
        others = await conn.fetch(
            "SELECT cell, current_fp, next_fp FROM loom_router.cells WHERE cell<>$1 ORDER BY cell",
            cell_name,
        )
        found = _attaching(row, str(others[0]["cell"]) if others else None)
        if reason := control_refusal(found, expected):
            raise OperatorRefused(reason)
        if recorded_keys != keys_fp:
            raise OperatorRefused(
                "cell root-secret fingerprint changed; configuration update refuses"
            )
        registered = {name: getattr(found, name) for name in ("bucket", "bucket_prefix")}
        registered.update(
            cell=cell_name, deployment_id=found.row.deployment_id, host=found.row.host
        )
        if reason := fingerprints.registration_pair_refusal(registered, current, pending):
            raise OperatorRefused(reason)
        for other in others:
            reserved = [
                other[name] for name in ("current_fp", "next_fp") if other[name] is not None
            ]
            if reason := fingerprints.reservation_refusal(accepted, reserved):
                raise OperatorRefused(f"cell {other['cell']!r}: {reason}")
        await conn.execute("SELECT pg_logical_emit_message(true, 'loom.control', '')")
        placed = await conn.fetch(
            "SELECT org, cell FROM loom_router.placements WHERE org=ANY($1::text[]) ORDER BY org",
            orgs,
        )
        elsewhere = next((row for row in placed if row["cell"] != cell_name), None)
        if elsewhere is not None:
            raise OperatorRefused(
                f"org {elsewhere['org']!r} is placed in cell {elsewhere['cell']!r}"
            )
        new = sorted(set(orgs) - {row["org"] for row in placed})
        if not found.row.is_first and found.row.state != "serving":
            if desired_orgs != prior_orgs:
                raise OperatorRefused("a later cell must be serving before configured orgs change")
        if set(new) & prior_orgs:
            if not (found.row.is_first and found.row.state == "attaching"):
                raise OperatorRefused(
                    "configured org placement is missing; "
                    "restore the directory metadata before updating"
                )
            # Original first-cell orgs retain attach's placement and quota protocol.
            new = sorted(set(new) - prior_orgs)
        if new:
            if reason := refusal(found.row):
                raise OperatorRefused(reason)
            if reason := _quota_refusal(found.row, len(new)):
                raise OperatorRefused(reason)
        return await _configuration_commit(conn, found, current, pending, new, action)

    return await _control_transaction(locked, seconds=CONTROL_MAX_S)


async def _configuration_commit(
    conn: Any,
    found: Attaching,
    current: dict[str, Any],
    pending: dict[str, Any] | None,
    new_orgs: list[str],
    action: str,
) -> Attaching:
    accepted_issuers = sorted(
        {
            issuer
            for value in (current, pending)
            if value is not None
            for issuer in value["fields"].get("bearer_issuers", [])
            if issuer
        }
    )
    written = await conn.fetchrow(
        "WITH inserted AS (INSERT INTO loom_router.placements (org, cell) "
        "SELECT org, $1 FROM unnest($2::text[]) AS org RETURNING org), "
        "placed_audit AS (INSERT INTO loom_router.audit (actor, action, detail) "
        "SELECT 'operator', 'place', jsonb_build_object('org', org, 'cell', $1::text) "
        "FROM inserted), "
        "changed AS (UPDATE loom_router.cells SET current_fp=$3, next_fp=$4, "
        "issuers=$5, placed=placed+(SELECT count(*) FROM inserted), updated_at=now() "
        f"WHERE cell=$1 RETURNING {_CELL_COLUMNS}, {', '.join(_ATTACH_EXTRA)}), "
        "changed_audit AS (INSERT INTO loom_router.audit (actor, action, detail) "
        "SELECT 'operator', 'configuration_' || $6::text, "
        "jsonb_build_object('cell', cell, 'placed', (SELECT count(*) FROM inserted)) FROM changed) "
        "SELECT * FROM changed",
        found.row.cell,
        new_orgs,
        current,
        pending,
        accepted_issuers if found.row.state == "serving" else [],
        action,
    )
    return _attaching(written, found.other)


async def _audit(conn: Any, action: str, detail: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO loom_router.audit (actor, action, detail) VALUES ('operator', $1, $2)",
        action,
        detail,
    )


async def attach_begin(cell_name: str, begin: Begin, *, expected: Control) -> Attaching:
    """`attach/begin`: write `attach_started_at` once, the fingerprints while the row has
    none, and the root-secret fingerprint on every call, audited with the allow lists.
    Idempotent: a rerun posting the same fingerprints writes only `keys_fp`."""

    async def change_locked(conn: Any, found: Attaching) -> Attaching:
        if reason := begin_refusal(found, begin):
            raise OperatorRefused(reason)
        await conn.execute(
            "UPDATE loom_router.cells SET attach_started_at = COALESCE(attach_started_at, now()), "
            "current_fp = $2, next_fp = $3, keys_fp = $4, updated_at = now() WHERE cell = $1",
            cell_name,
            begin.current_fp,
            begin.next_fp,
            begin.keys_fp,
        )
        await _audit(
            conn,
            "attach_begin",
            {
                "cell": cell_name,
                "deployment_id": begin.deployment_id,
                "keys_fp": begin.keys_fp,
                "fingerprints_written": found.row.current_fp is None,
                **begin.allowed,
            },
        )
        written = await conn.fetchrow(f"{_CELL_ATTACH} WHERE cell = $1", cell_name)
        return _attaching(written, None)

    return await _attach_change(cell_name, expected, change_locked)


async def attach_place(
    cell_name: str, deployment_id: str, orgs: list[str], *, expected: Control
) -> tuple[int, int]:
    """`attach/place`: place a batch of the first cell's orgs, once its directory mode is
    `enforced`, past its closed mark and quota (the orgs are the cell's already); the count
    it placed now and the cell's count. An org already placed in the cell is skipped, so a
    rerun places nothing twice; one placed in another cell refuses the batch, naming it, and
    writes nothing. A fixed number of statements, whatever the batch's size. The router
    cannot read the cell's Postgres: only attach, which posts this once the cell's mode
    reads `enforced`, judges that."""
    _refuse_long(orgs)
    batch = sorted(set(orgs))

    async def change_locked(conn: Any, found: Attaching) -> tuple[int, int]:
        if reason := attach_refusal(found, deployment_id, enforced=True):
            raise OperatorRefused(reason)
        new = await conn.fetch(
            "INSERT INTO loom_router.placements (org, cell) SELECT o, $2 FROM unnest($1::text[]) "
            "AS o ON CONFLICT DO NOTHING RETURNING org",
            batch,
            cell_name,
        )
        elsewhere = await conn.fetchrow(
            "SELECT org, cell FROM loom_router.placements WHERE org = ANY($1::text[]) "
            "AND cell <> $2 ORDER BY org LIMIT 1",
            batch,
            cell_name,
        )
        if elsewhere is not None:
            # A backstop: attach refuses while another cell row exists, under the lock every
            # writer of the rows takes. Raised inside the transaction, so the inserts roll
            # back.
            raise OperatorRefused(
                f"org {elsewhere['org']!r} is placed in cell {elsewhere['cell']!r}"
            )
        placed = await conn.fetchval(
            "UPDATE loom_router.cells SET placed = placed + $2 WHERE cell = $1 RETURNING placed",
            cell_name,
            len(new),
        )
        # One row per org placed, and one for the call, so a batch that places nothing (a
        # rerun) is audited too.
        await conn.execute(
            "INSERT INTO loom_router.audit (actor, action, detail) "
            "SELECT 'operator', 'place', jsonb_build_object('org', o, 'cell', $2::text) "
            "FROM unnest($1::text[]) AS o UNION ALL SELECT 'operator', 'attach_place', "
            "jsonb_build_object('cell', $2::text, 'named', $3::int, 'placed', $4::int)",
            [str(r["org"]) for r in new],
            cell_name,
            len(batch),
            len(new),
        )
        return len(new), int(placed)

    return await _attach_change(cell_name, expected, change_locked)


async def attach_enforced(cell_name: str, deployment_id: str, *, expected: Control) -> Attaching:
    """`attach/enforced`: record directory mode `enforced` (registration wrote `recording`),
    after which the cell places new orgs live; idempotent."""

    async def change_locked(conn: Any, found: Attaching) -> Attaching:
        if reason := attach_refusal(found, deployment_id):
            raise OperatorRefused(reason)
        await conn.execute(
            "UPDATE loom_router.cells SET mode = 'enforced', mode_set_at = now(), "
            "updated_at = now() WHERE cell = $1 AND mode <> 'enforced'",
            cell_name,
        )
        await _audit(
            conn,
            "attach_enforced",
            {"cell": cell_name, "changed": found.row.mode != "enforced"},
        )
        written = await conn.fetchrow(f"{_CELL_ATTACH} WHERE cell = $1", cell_name)
        return _attaching(written, None)

    return await _attach_change(cell_name, expected, change_locked)


def issuers(row: Cell) -> list[str]:
    """The bearer-token issuers the cell's fingerprints accept (`bearer_issuers` of
    `current` and `next`), which routing sends the shared host's tokens by."""
    found: set[str] = set()
    for value in (row.current_fp, row.next_fp):
        fields = value.get("fields") if isinstance(value, dict) else None
        listed = fields.get("bearer_issuers") if isinstance(fields, dict) else None
        if isinstance(listed, list):
            found.update(i for i in listed if isinstance(i, str) and i)
    return sorted(found)


async def attach_serving(cell_name: str, deployment_id: str, *, expected: Control) -> Attaching:
    """`attach/serving`: mark the first cell `serving`, with the issuers its fingerprints
    accept computed from the row as locked. A `serving` row is answered as it is."""

    async def change_locked(conn: Any, found: Attaching) -> Attaching:
        row = found.row
        if found.served(deployment_id):
            await _audit(conn, "attach_serving", {"cell": cell_name, "serving_already": True})
            return found
        if reason := attach_refusal(found, deployment_id, enforced=True):
            raise OperatorRefused(reason)
        accepted = issuers(row)
        await conn.execute(
            "UPDATE loom_router.cells SET state = 'serving', issuers = $2, updated_at = now() "
            "WHERE cell = $1",
            cell_name,
            accepted,
        )
        await _audit(conn, "attach_serving", {"cell": cell_name, "issuers": accepted})
        written = await conn.fetchrow(f"{_CELL_ATTACH} WHERE cell = $1", cell_name)
        return _attaching(written, None)

    return await _attach_change(cell_name, expected, change_locked)


def remove_refusal(cell_name: str, state: str, placed: int) -> str:
    """Why a cell's row may not be removed ('' when it may): only while it is `attaching`
    with no placement. `loom cell remove` judges the row it read the same way first."""
    if state != "attaching":
        return f"cell {cell_name!r} is {state}: a cell stays attached"
    if placed:
        return (
            f"cell {cell_name!r} holds {placed} placement(s): its attach goes forward, never back"
        )
    return ""


async def remove(cell_name: str, *, expected: Control) -> Cell:
    """`DELETE /cells/v1/operator/cells/<cell>` (`loom cell remove`): remove a cell's row
    and its host rows, audited, while it is `attaching` with no placement, whatever its
    directory mode. A placement in flight either commits first (and the removal refuses) or
    finds no row. The router cannot read the cell's stores: `loom cell remove` checks that a
    first cell's Postgres reads `open` first, and a direct call skips that check, leaving a
    first cell still prepared answering its new orgs 503 and unable to register again until
    the full back-out has run."""

    async def change_locked(conn: Any, found: Attaching) -> Cell:
        row = found.row
        if reason := remove_refusal(cell_name, row.state, row.placed):
            raise OperatorRefused(reason)
        await conn.execute("DELETE FROM loom_router.hosts WHERE cell = $1", cell_name)
        await conn.execute("DELETE FROM loom_router.cells WHERE cell = $1", cell_name)
        await _audit(
            conn,
            "remove",
            {
                "cell": cell_name,
                "is_first": row.is_first,
                "host": row.host,
                "deployment_id": row.deployment_id,
                "mode": row.mode,
            },
        )
        return row

    return await _attach_change(cell_name, expected, change_locked)


async def lookup(org: str) -> tuple[str, str] | None:
    """The (cell, host) an org is placed in, or None."""
    async with _reading(), _connection(lookup=True) as conn:
        row = await conn.fetchrow(
            "SELECT c.cell, c.host FROM loom_router.placements AS p "
            "JOIN loom_router.cells AS c USING (cell) WHERE p.org = $1",
            org,
        )
    return (str(row["cell"]), str(row["host"])) if row else None


async def ping() -> None:
    """Read every cells column this build reads (placement, lookup and routing), so a
    router rolled out before `loom router migrate` stays unready instead of failing them."""
    async with _reading(), _connection() as conn:
        await conn.fetch(f"{_CELL} LIMIT 0")
        await conn.fetch(f"{_ROUTING} LIMIT 0")


@asynccontextmanager
async def _reading() -> AsyncIterator[None]:
    """Turn a failed statement or an unreachable directory into DirectoryUnavailable."""
    import asyncpg

    try:
        yield
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        raise DirectoryUnavailable(f"the directory could not be read: {type(e).__name__}") from e
