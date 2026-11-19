"""Update snapshots and acceptance writes on the exact ATTACH-owning backend."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from server.cli import cell_control
from server.core import cell
from server.router import fingerprints


@dataclass(frozen=True)
class State:
    deployment_id: str
    is_first: bool
    cell: str
    mode: str
    current: dict[str, Any]
    pending: dict[str, Any] | None

    def identity(self) -> tuple[Any, ...]:
        return (
            self.deployment_id,
            self.is_first,
            self.cell,
            self.mode,
            json.dumps((self.current, self.pending), sort_keys=True),
        )


async def read(conn: Any, name: str) -> State:
    row = await conn.fetchrow(
        "SELECT a.deployment_id, f.is_first, f.cell, m.mode, s.current_fp, s.next_fp "
        "FROM loom.authority a "
        "LEFT JOIN loom.cell_first f ON true LEFT JOIN loom.tenancy_mode m ON true "
        "LEFT JOIN loom.cell_self s ON true"
    )
    if (
        row is None
        or not row["deployment_id"]
        or type(row["is_first"]) is not bool
        or row["cell"] != name
        or row["mode"] not in ("recording", "enforced")
    ):
        raise cell_control._refused(
            "update needs complete prepared state naming this cell; prepare or restore it first"
        )
    current, pending = cell.json_object(row["current_fp"]), cell.json_object(row["next_fp"])
    if not fingerprints.readable(current) or (
        pending is not None and not fingerprints.readable(pending)
    ):
        raise cell_control._refused("update needs readable local current/next fingerprints")
    assert current is not None
    return State(
        str(row["deployment_id"]),
        row["is_first"],
        name,
        row["mode"],
        current,
        pending,
    )


def paired(state: State, registered: dict[str, Any]) -> None:
    if (
        state.deployment_id != registered["deployment_id"]
        or state.is_first != registered["is_first"]
    ):
        raise cell_control._refused(
            "the prepared local identity differs from the directory registration"
        )
    if registered["current_fp"] is not None:
        accepted = {
            json.dumps(value, sort_keys=True)
            for value in (registered["current_fp"], registered["next_fp"])
            if value is not None
        }
        if any(
            json.dumps(value, sort_keys=True) not in accepted
            for value in (state.current, state.pending)
            if value is not None
        ):
            raise cell_control._refused(
                "local fingerprints differ from the directory reservation; restore the "
                "recorded pair"
            )


async def write(
    owner: cell_control.Owner,
    expected: State,
    registered: dict[str, Any],
    current: dict[str, Any],
    pending: dict[str, Any] | None,
    placed_orgs: list[str],
) -> State:
    cell_control.preconditions(registered)

    async def change() -> State:
        conn = owner.conn
        await cell_control.paired(conn, registered)
        actual = await read(conn, expected.cell)
        if actual.identity() != expected.identity():
            raise cell_control._refused(
                "local acceptance changed; stop and rerun with fresh observations"
            )
        if placed_orgs:
            await conn.execute(
                "INSERT INTO loom.tenancy(org) SELECT org FROM unnest($1::text[]) AS org "
                "ON CONFLICT DO NOTHING",
                placed_orgs,
            )
        result = await conn.execute(
            "UPDATE loom.cell_self SET current_fp=$1, next_fp=$2, updated_at=now() WHERE id=1",
            current,
            pending,
        )
        if result != "UPDATE 1":
            raise cell_control._refused(
                "local acceptance row disappeared; restore it before updating"
            )
        return await read(conn, expected.cell)

    return await owner.transaction(change)
