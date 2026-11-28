"""Durable cell metadata batches on restore's marked operator session."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from server.cli import cell_attach_pg as apg
from server.core import cell, pg
from server.settings import settings


async def identity(conn: Any, descriptor: dict[str, Any] | None = None) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT authority, epoch, deployment_id FROM loom.authority")
    if row is None or row["authority"] != "postgres" or not row["deployment_id"]:
        raise ValueError("the local Postgres has no recorded Postgres authority")
    if descriptor is not None and row["deployment_id"] != descriptor["deployment_id"]:
        raise ValueError("the local Postgres names another directory deployment")
    if await conn.fetchval("SELECT to_regclass('loom.cell_first') IS NOT NULL"):
        first = await conn.fetchrow("SELECT is_first, cell FROM loom.cell_first")
        if first is not None and (
            first["cell"] != settings.cell
            or (descriptor is not None and first["is_first"] != descriptor["is_first"])
        ):
            raise ValueError("the local first row names another cell")
    return dict(row)


async def checked(conn: Any, descriptor: dict[str, Any], recorded: dict[str, Any]) -> None:
    if not await apg.lock_held(conn):
        raise ValueError("restore lost its operator lock; repeat the command")
    pg.same_deployment(await identity(conn, descriptor), recorded)


async def transaction(conn: Any, work: Callable[[], Awaitable[Any]]) -> Any:
    """A no-op batch acknowledges fresh WAL; the session owner handles failure."""
    tx = conn.transaction()
    await tx.start()
    result = await work()
    await conn.execute(pg._DURABLE_MESSAGE)
    await tx.commit()
    return result


async def tenancy(
    conn: Any, descriptor: dict[str, Any], recorded: dict[str, Any], orgs: list[str]
) -> int:
    async def write() -> int:
        await checked(conn, descriptor, recorded)
        rows = await conn.fetch(
            "INSERT INTO loom.tenancy (org) SELECT unnest($1::text[]) "
            "ON CONFLICT (org) DO NOTHING RETURNING org",
            orgs,
        )
        return len(rows)

    return int(await transaction(conn, write))


async def read_rows(conn: Any) -> dict[str, Any]:
    return dict(await conn.fetchrow(cell._READ))


def target_mode(local: str | None, descriptor: dict[str, Any]) -> str:
    return (
        "enforced"
        if (local == "enforced" or descriptor["mode"] == "enforced" or not descriptor["is_first"])
        else "recording"
    )


async def state(conn: Any, descriptor: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
    async def write() -> dict[str, Any]:
        await checked(conn, descriptor, recorded)
        before = await read_rows(conn)
        mode = target_mode(before["mode"], descriptor)
        await conn.execute(
            "INSERT INTO loom.cell_first (is_first, cell) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            descriptor["is_first"],
            descriptor["cell"],
        )
        await conn.execute(
            "INSERT INTO loom.tenancy_mode (mode) VALUES ($1) ON CONFLICT (id) DO UPDATE "
            "SET mode = CASE WHEN loom.tenancy_mode.mode = 'enforced' THEN 'enforced' "
            "ELSE EXCLUDED.mode END",
            mode,
        )
        own = before["own"]
        if descriptor["current_fp"] is not None:
            own = {"current_fp": descriptor["current_fp"], "next_fp": descriptor["next_fp"]}
            await conn.execute(
                "INSERT INTO loom.cell_self (current_fp, next_fp) VALUES ($1, $2) "
                "ON CONFLICT (id) DO UPDATE SET current_fp=EXCLUDED.current_fp, "
                "next_fp=EXCLUDED.next_fp",
                descriptor["current_fp"],
                descriptor["next_fp"],
            )
        after = await read_rows(conn)
        if (
            after["first"] != {"cell": descriptor["cell"], "is_first": descriptor["is_first"]}
            or after["mode"] != mode
            or after["own"] != own
        ):
            raise ValueError("restored cell rows differ from the planned repair")
        return after

    return dict(await transaction(conn, write))


class LocalTenancy:
    """Ordered pages keep validation proportional to this cell, with bounded memory."""

    def __init__(self, conn: Any):
        self.conn = conn
        self.after: str | None = None
        self.rows: list[str] = []
        self.index = 0
        self.done = False
        self.extra = 0

    async def peek(self) -> str | None:
        if self.index == len(self.rows) and not self.done:
            predicate = " AND org > $1" if self.after is not None else ""
            args = [self.after] if self.after is not None else []
            found = await self.conn.fetch(
                "SELECT org FROM loom.tenancy WHERE org NOT IN ('', '-', '*') "
                + predicate
                + " ORDER BY org LIMIT 500",
                *args,
            )
            self.rows = [r["org"] for r in found]
            self.index = 0
            self.done = len(self.rows) < 500
        return self.rows[self.index] if self.index < len(self.rows) else None

    def advance(self) -> None:
        self.after = self.rows[self.index]
        self.index += 1

    async def compare(self, orgs: list[str], descriptor: dict[str, Any], *, last: bool) -> None:
        for org in orgs:
            while (local := await self.peek()) is not None and local < org:
                self.keep_extra(local, descriptor)
                self.advance()
            if await self.peek() != org:
                raise ValueError(f"directory org {org!r} has no repaired local tenancy")
            self.advance()
        if last:
            while (local := await self.peek()) is not None:
                self.keep_extra(local, descriptor)
                self.advance()

    def keep_extra(self, org: str, descriptor: dict[str, Any]) -> None:
        if not (
            descriptor["is_first"]
            and descriptor["state"] == "attaching"
            and descriptor["sole_cell"]
        ):
            raise ValueError(
                f"local tenancy {org!r} is absent from this cell's permanent placements"
            )
        self.extra += 1
