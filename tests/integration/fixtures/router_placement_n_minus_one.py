"""Frozen pre-generation router placement/query code for schema coexistence tests.

The serving statements and row model are copied from the checkpoint before the
control generation column. Connection acquisition/error handling are injected from
the test directory; no current placement projection or writer is imported.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from server.core import cell as cell_state
from server.router.directory import OrgTooLong, PlacementRefused, _connection, _reading

_PLACE_LOCK = 0x6C726F70  # "lrop"


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


_CELL_COLUMNS = (
    "cell, is_first, host, state, mode, closed, placements_max, placed, credential_sha256, "
    "credential_next_sha256, deployment_id, current_fp, next_fp"
)

_CELL = f"SELECT {_CELL_COLUMNS} FROM loom_router.cells"

_WINNER = (
    "SELECT p.cell, c.deployment_id FROM loom_router.placements AS p "
    "JOIN loom_router.cells AS c USING (cell) WHERE p.org = $1"
)


def _cell(row: Any) -> Cell:
    return Cell(**dict(row))


async def place(org: str, cell_name: str, refusal: Callable[[Cell], str]) -> tuple[str, str]:
    """Place `org` in `cell_name` unless it is placed already; the (cell, deployment id)
    that holds it. An org placed already answers its cell, whatever the cell's state.
    Otherwise the cell's row is locked, the winner read again (an attach batch holds the row
    lock, not the org's), and the row judged (`refusal`, then its quota), so a change to it
    waits for the placement and the quota holds across router replicas; a refused placement
    raises PlacementRefused and writes nothing, and a new org name longer than
    `cell.ORG_MAX_BYTES` raises OrgTooLong (one placed before the limit answers its cell, as
    any placed org does)."""
    async with _reading(), _connection() as conn, conn.transaction():
        # Placements of one org run one at a time, whichever cell asks: the later one
        # reads the earlier one's winner.
        await conn.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))", _PLACE_LOCK, org)
        row = await conn.fetchrow(_WINNER, org)
        if row is None:
            _refuse_long([org])
            locked = await conn.fetchrow(f"{_CELL} WHERE cell = $1 FOR UPDATE", cell_name)
            if locked is None:
                raise PlacementRefused(f"cell {cell_name!r} is not in the directory")
            # An attach batch of this cell, which holds the row lock and not the org's, may
            # have placed the org since the first read: it is answered as placed.
            row = await conn.fetchrow(_WINNER, org)
            if row is None:
                await _place_one(conn, org, cell_name, _cell(locked), refusal)
                row = await conn.fetchrow(_WINNER, org)
    return str(row["cell"]), str(row["deployment_id"])


async def _place_one(
    conn: Any, org: str, cell_name: str, candidate: Cell, refusal: Callable[[Cell], str]
) -> None:
    """Judge the locked row (`refusal`, then its quota) and insert the placement; its count
    and audit row only when the insert wrote it."""
    if reason := refusal(candidate):
        raise PlacementRefused(reason)
    quota = candidate.placements_max
    if quota is not None and candidate.placed >= quota:
        raise PlacementRefused(f"cell {cell_name!r} holds its quota of orgs")
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
    await conn.execute(
        "INSERT INTO loom_router.audit (actor, action, detail) VALUES ($1, 'place', $2)",
        f"cell:{cell_name}",
        {"org": org, "cell": cell_name},
    )


def _refuse_long(orgs: Iterable[str]) -> None:
    """OrgTooLong for the first org longer than `cell.ORG_MAX_BYTES`: the one check every
    route that inserts a placement runs, so no route places such an org."""
    for org in orgs:
        if reason := cell_state.org_name_refusal(org):
            raise OrgTooLong(reason)


async def lookup(org: str) -> tuple[str, str] | None:
    """The (cell, host) an org is placed in, or None."""
    async with _reading(), _connection(lookup=True) as conn:
        row = await conn.fetchrow(
            "SELECT c.cell, c.host FROM loom_router.placements AS p "
            "JOIN loom_router.cells AS c USING (cell) WHERE p.org = $1",
            org,
        )
    return (str(row["cell"]), str(row["host"])) if row else None
