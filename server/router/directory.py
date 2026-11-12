"""The router's directory, in its own Postgres database (`LOOM_ROUTER_PG_DSN`):
`loom_router.cells`, `placements`, `hosts` and `audit`
(`schemas/router_migrations/`, docs/architecture/cells.md, "The router").

Every statement the router runs against it is here. Placements of one org run one
at a time under a transaction-scoped lock on the org (`_PLACE_LOCK`, which every path
that places takes), so two cells placing one org at once agree on the winner, and a
retry of the same call answers the same.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from server.core.paths import ROUTER_MIGRATIONS_DIR
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
# The longest a limit or credential change can take: the connection wait, then eight
# statements (begin, lock, row, check, update, audit, read-back, commit) and the pool's
# reset of the connection as it is released, at the client bound each. Registration has
# its own bound, below.
OPERATOR_CHANGE_MAX_S = (  # not a setting: derived from the bound above
    STATEMENT_TIMEOUT_S + (8 + 1) * (STATEMENT_TIMEOUT_S + 1)
)
# The longest a registration's directory write can take: the connection wait, then
# thirteen statements (begin, lock, row, five refusal reads, three inserts, read-back,
# commit); and its read before the probe: the connection wait, then six statements (the
# row, the first cell, then the credential, deployment id, host and bucket reads). Each
# adds the pool's reset of its connection on release (asyncpg's, at the client bound).
# tests/unit/test_cell_register.py counts the statements against the code.
REGISTER_STATEMENTS, REFUSAL_READ_STATEMENTS = 13, 6
REGISTER_MAX_S = STATEMENT_TIMEOUT_S + (REGISTER_STATEMENTS + 1) * (STATEMENT_TIMEOUT_S + 1)
REFUSAL_READ_MAX_S = STATEMENT_TIMEOUT_S + (REFUSAL_READ_STATEMENTS + 1) * (STATEMENT_TIMEOUT_S + 1)
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


def _require_dsn() -> str:
    if not settings.router_pg_dsn:
        raise DirectoryUnavailable("no directory: set LOOM_ROUTER_PG_DSN")
    return settings.router_pg_dsn


async def _init(conn: Any) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def pool() -> Any:
    """The directory's pool, opened on first use."""
    global _pool
    if _pool is not None:
        return _pool
    import asyncpg

    async with _opening:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                _require_dsn(),
                min_size=1,
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
    async with AsyncExitStack() as stack:
        async with asyncio.timeout(STATEMENT_TIMEOUT_S):
            if lookup:
                await _lookups.acquire()
                stack.callback(_lookups.release)
            conn = await stack.enter_async_context((await pool()).acquire())
        yield conn


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
    "credential_next_sha256, deployment_id, current_fp, next_fp"
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
) -> Cell:
    """`loom cell limit`: close or reopen a cell to new orgs and set or clear its
    placement quota, on its row alone, audited; the cell's stores are not touched. A
    field left UNSET is not written (a quota of None clears it)."""

    if closed is UNSET and placements_max is UNSET:
        raise ValueError("set_limit sets closed, placements_max or both")

    async def change(conn: Any, row: Cell) -> dict[str, Any]:
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

    return await _operator_change(cell_name, "limit", change)


async def rotate_credential(cell_name: str, step: str, sha256: str = "") -> Cell:
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

    return await _operator_change(cell_name, "credential", change)


async def _operator_change(
    cell_name: str, action: str, change: Callable[[Any, Cell], Awaitable[dict[str, Any]]]
) -> Cell:
    async with _reading(), _connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _DIRECTORY_LOCK)
        locked = await conn.fetchrow(f"{_CELL} WHERE cell = $1 FOR UPDATE", cell_name)
        if locked is None:
            raise NoSuchCell(f"cell {cell_name!r} is not in the directory")
        detail = await change(conn, _cell(locked))
        await conn.execute(
            "INSERT INTO loom_router.audit (actor, action, detail) VALUES ('operator', $1, $2)",
            action,
            {"cell": cell_name, **detail},
        )
        row = await conn.fetchrow(f"{_CELL} WHERE cell = $1", cell_name)
    return _cell(row)


@dataclass(frozen=True)
class Registration:
    """What a cell's preparation registers: the cell's host (as `normalise_host` writes
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
    """A cell's preparation: the cell's row as `attaching` (mode `recording` on the first
    cell, `enforced` on a later one), its host row and an audit row, in one transaction
    under the directory lock; and whether this call wrote them.

    A rerun (a row of the same name) answers that row, or is refused when its first-cell
    standing, deployment id, host, bucket, prefix or addresses differ from the row's or its
    hash is neither the row's current nor next (`_rerun_refusal`); it runs none of the
    checks below.

    Refused: a second first cell, a later cell before the first, a first cell with no
    upstream to copy, a credential hash any cell holds as current or next, a deployment
    id or host another cell holds, and a bucket prefix that shares objects with another
    cell's in a bucket of the same name."""
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
            f"cell {cell!r} is registered with probe address {held['probe_url']}: register "
            "with that probe_url"
        )
    if reg.is_first and reg.upstream_url and held["upstream_url"] != reg.upstream_url:
        return (
            f"cell {cell!r} is registered with upstream {held['upstream_url']}: set this router "
            "replica's LOOM_ROUTER_UPSTREAM_URL to it"
        )
    return ""


def _order_refusal(reg: Registration, first: str | None) -> str:
    """Why a new cell cannot register given the first cell's name (`first`, None before
    one is), '' when it can: one first cell, registered before any other, with the
    router's upstream to copy."""
    if reg.is_first and first is not None:
        return f"cell {first!r} is the deployment's first cell already"
    if not reg.is_first and first is None:
        return "the first cell is registered before any other"
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


async def _refuse_registration(conn: Any, reg: Registration) -> None:
    first = await conn.fetchval("SELECT cell FROM loom_router.cells WHERE is_first")
    if refusal := _order_refusal(reg, first):
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
    """The first cell's row when it is `cell_name`, else None: the state read a first
    cell in `recording` makes. Not a lookup: it takes no lookup slot."""
    async with _reading(), _connection() as conn:
        row = await conn.fetchrow(f"{_CELL} WHERE is_first AND cell = $1", cell_name)
    return _cell(row) if row else None


async def place(org: str, cell_name: str, refusal: Callable[[Cell], str]) -> tuple[str, str]:
    """Place `org` in `cell_name` unless it is placed already; the (cell, deployment
    id) that holds it. An org placed already answers its cell, whatever the cell's
    state. Otherwise the cell's row is locked and judged (`refusal`, then its quota),
    so a change to it waits for the placement and the quota holds across router
    replicas; a refused placement raises PlacementRefused and writes nothing."""
    async with _reading(), _connection() as conn, conn.transaction():
        # Placements of one org run one at a time, whichever cell asks: the later one
        # reads the earlier one's winner.
        await conn.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))", _PLACE_LOCK, org)
        row = await conn.fetchrow(_WINNER, org)
        if row is None:
            locked = await conn.fetchrow(f"{_CELL} WHERE cell = $1 FOR UPDATE", cell_name)
            if locked is None:
                raise PlacementRefused(f"cell {cell_name!r} is not in the directory")
            candidate = _cell(locked)
            if reason := refusal(candidate):
                raise PlacementRefused(reason)
            quota = candidate.placements_max
            if quota is not None and candidate.placed >= quota:
                raise PlacementRefused(f"cell {cell_name!r} holds its quota of orgs")
            await conn.execute(
                "INSERT INTO loom_router.placements (org, cell) VALUES ($1, $2)", org, cell_name
            )
            await conn.execute(
                "UPDATE loom_router.cells SET placed = placed + 1 WHERE cell = $1", cell_name
            )
            await conn.execute(
                "INSERT INTO loom_router.audit (actor, action, detail) VALUES ($1, 'place', $2)",
                f"cell:{cell_name}",
                {"org": org, "cell": cell_name},
            )
            row = await conn.fetchrow(_WINNER, org)
    return str(row["cell"]), str(row["deployment_id"])


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
