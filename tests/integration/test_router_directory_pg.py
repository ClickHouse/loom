"""The router's directory against a real Postgres (server/router/directory.py,
schemas/router_migrations/): its migrations, one org placed once by two cells asking at
the same moment, the quota held under concurrency, and placements no statement can
change. Needs LOOM_ROUTER_PG_DSN: a database of its own, never a cell's."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest

from server.router import directory
from server.settings import settings


@pytest.fixture
def dsn(monkeypatch: pytest.MonkeyPatch) -> str:
    value = os.environ.get("LOOM_ROUTER_PG_DSN") or pytest.skip("LOOM_ROUTER_PG_DSN not set")
    monkeypatch.setattr(settings, "router_pg_dsn", value)
    monkeypatch.setattr(directory, "_pool", None)
    return value


async def _cell(conn, name: str, *, limit: int | None = None) -> None:
    await conn.execute(
        "INSERT INTO loom_router.cells (cell, host, state, mode, placements_max, "
        "credential_sha256, deployment_id) VALUES ($1, $2, 'serving', 'enforced', $3, $4, $5)",
        name,
        f"{name}.example.test",
        limit,
        hashlib.sha256(name.encode()).hexdigest(),
        f"dep-{name}",
    )


def _never(_row: directory.Cell) -> str:
    return ""


@pytest.mark.asyncio
async def test_the_directory_places_once_and_holds_the_quota(dsn: str) -> None:
    import asyncpg

    assert isinstance(await directory.migrate(), list)
    assert await directory.migrate() == []  # applied once
    # Every pool connection commits synchronously under read committed, and the server
    # bounds its statements and idle transactions.
    async with (await directory.pool()).acquire() as pooled:
        shown = {
            name: await pooled.fetchval(f"SHOW {name}")
            for name in (
                "synchronous_commit",
                "default_transaction_isolation",
                "statement_timeout",
                "idle_in_transaction_session_timeout",
            )
        }
    assert shown == {
        "synchronous_commit": "on",
        "default_transaction_isolation": "read committed",
        "statement_timeout": "10s",
        "idle_in_transaction_session_timeout": "20s",
    }
    a, b, q = (f"it-{uuid.uuid4().hex[:8]}" for _ in range(3))
    conn = await asyncpg.connect(dsn)
    try:
        await _cell(conn, a)
        await _cell(conn, b)
        await _cell(conn, q, limit=3)
        # Two cells place one org at the same moment: the org's lock is held while both
        # wait, so both run with no placement yet, and both read back one winner.
        org = f"org-{uuid.uuid4().hex[:8]}"
        holder = await asyncpg.connect(dsn)
        try:
            tx = holder.transaction()
            await tx.start()
            await holder.execute(
                "SELECT pg_advisory_xact_lock($1, hashtext($2))", directory._PLACE_LOCK, org
            )
            racing = asyncio.gather(
                directory.place(org, a, _never), directory.place(org, b, _never)
            )
            # Both placements wait on the org's lock before it is released.
            for _ in range(100):
                waiting = await conn.fetchval(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid::bigint = $1 AND NOT granted",
                    directory._PLACE_LOCK,
                )
                if waiting == 2:
                    break
                await asyncio.sleep(0.05)
            assert waiting == 2
            await tx.rollback()
            winners = await racing
        finally:
            await holder.close()
        assert winners[0] == winners[1] and winners[0][0] in (a, b)
        assert winners[0][1] == f"dep-{winners[0][0]}"
        assert await directory.lookup(org) == (winners[0][0], f"{winners[0][0]}.example.test")
        audit = await conn.fetch(
            "SELECT actor FROM loom_router.audit WHERE action = 'place' AND detail->>'org' = $1",
            org,
        )
        assert [r["actor"] for r in audit] == [f"cell:{winners[0][0]}"]
        # Ten concurrent placements in a cell with room for three: three are written.
        results = await asyncio.gather(
            *(directory.place(f"{q}-{i}", q, _never) for i in range(10)), return_exceptions=True
        )
        assert sum(isinstance(r, tuple) for r in results) == 3
        assert sum(isinstance(r, directory.PlacementRefused) for r in results) == 7
        assert await conn.fetchval("SELECT placed FROM loom_router.cells WHERE cell = $1", q) == 3
        held = await conn.fetchval("SELECT count(*) FROM loom_router.placements WHERE cell = $1", q)
        assert held == 3
        # An org placed already answers its cell, whatever the quota or the cell's state.
        placed = next(f"{q}-{i}" for i, r in enumerate(results) if isinstance(r, tuple))
        assert await directory.place(placed, q, lambda _r: "closed") == (q, f"dep-{q}")
        # A refusal judged on the locked row writes nothing.
        with pytest.raises(directory.PlacementRefused, match="closed"):
            await directory.place(f"new-{a}", a, lambda _r: "closed")
        assert await directory.lookup(f"new-{a}") is None
        # A credential's hash names one cell: a second cell cannot record it.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO loom_router.cells (cell, host, state, credential_sha256, "
                "deployment_id) VALUES ($1, $2, 'attaching', $3, $4)",
                f"dup-{a}",
                f"dup-{a}.example.test",
                hashlib.sha256(a.encode()).hexdigest(),
                f"dep-dup-{a}",
            )
        # A placement is written once: no update, delete or truncate.
        for sql, args in (
            ("UPDATE loom_router.placements SET cell = cell WHERE org = $1", (org,)),
            ("DELETE FROM loom_router.placements WHERE org = $1", (org,)),
            ("TRUNCATE loom_router.placements", ()),
        ):
            with pytest.raises(asyncpg.RaiseError):
                await conn.execute(sql, *args)
    finally:
        await conn.close()
        await directory.close()


@pytest.mark.asyncio
async def test_the_directory_never_migrates_into_a_memory_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    monkeypatch.setattr(settings, "router_pg_dsn", memory)
    with pytest.raises(directory.DirectoryUnavailable, match="memory authority"):
        await directory.migrate()
