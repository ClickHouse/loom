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
import json
from collections.abc import AsyncIterator, Callable
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
    """What the placement API needs of a cell's row."""

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


_CELL = (
    "SELECT cell, is_first, host, state, mode, closed, placements_max, placed, credential_sha256, "
    "deployment_id, current_fp, next_fp FROM loom_router.cells"
)


_WINNER = (
    "SELECT p.cell, c.deployment_id FROM loom_router.placements AS p "
    "JOIN loom_router.cells AS c USING (cell) WHERE p.org = $1"
)


def _cell(row: Any) -> Cell:
    return Cell(**dict(row))


async def credentials() -> dict[str, str]:
    """Every cell's credential hash, by cell name."""
    async with _reading(), _connection() as conn:
        rows = await conn.fetch("SELECT cell, credential_sha256 FROM loom_router.cells")
    return {str(r["cell"]): str(r["credential_sha256"]) for r in rows}


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
    async with _reading(), _connection() as conn:
        await conn.fetchval("SELECT 1")


@asynccontextmanager
async def _reading() -> AsyncIterator[None]:
    """Turn a failed statement or an unreachable directory into DirectoryUnavailable."""
    import asyncpg

    try:
        yield
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        raise DirectoryUnavailable(f"the directory could not be read: {type(e).__name__}") from e
