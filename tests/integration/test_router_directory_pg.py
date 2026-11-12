"""The router's directory against a real Postgres (server/router/directory.py,
schemas/router_migrations/): its migrations, one org placed once by two cells asking at
the same moment, the quota held under concurrency, placements no statement can change,
readiness on the columns the build reads, and the operator's changes with their audit.
Needs LOOM_ROUTER_PG_DSN: a database of its own, never a cell's."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from typing import Any
from unittest.mock import ANY
from urllib.parse import urlsplit, urlunsplit

import pytest

from server.router import directory, routing
from server.settings import settings


@pytest.fixture
def dsn(monkeypatch: pytest.MonkeyPatch) -> str:
    value = os.environ.get("LOOM_ROUTER_PG_DSN") or pytest.skip("LOOM_ROUTER_PG_DSN not set")
    monkeypatch.setattr(settings, "router_pg_dsn", value)
    monkeypatch.setattr(directory, "_pool", None)
    return value


@pytest.fixture
async def own_dsn(dsn: str, monkeypatch: pytest.MonkeyPatch):
    """A directory database of the test's own: a test that registers a first cell sees no
    other test's first cell, and no other test sees its."""
    import asyncpg

    name = f"loom_router_t{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        own = urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))  # its query kept
        monkeypatch.setattr(settings, "router_pg_dsn", own)
        monkeypatch.setattr(directory, "_pool", None)
        try:
            yield own
        finally:
            await directory.close()
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    finally:
        await admin.close()


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
        # Routing reads every cell with its upstream address ('' until attach writes it).
        cells, _aliases = await directory.routing()
        routed = {c["cell"]: c for c in cells}
        assert routed[a]["upstream_url"] == "" and routed[a]["host"] == f"{a}.example.test"
        # The state read answers only the first cell: none of these, until one is marked.
        assert await directory.first_cell_named(a) is None
        earlier = await conn.fetchval("SELECT cell FROM loom_router.cells WHERE is_first")
        await conn.execute("UPDATE loom_router.cells SET is_first = false WHERE is_first")
        try:
            await conn.execute("UPDATE loom_router.cells SET is_first = true WHERE cell = $1", a)
            row = await directory.first_cell_named(a)
            assert row is not None and row.is_first and row.cell == a
            assert await directory.first_cell_named(b) is None
        finally:
            await conn.execute("UPDATE loom_router.cells SET is_first = false WHERE cell = $1", a)
            if earlier is not None:
                await conn.execute(
                    "UPDATE loom_router.cells SET is_first = true WHERE cell = $1", earlier
                )
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
async def test_the_router_is_unready_without_a_column_it_reads(dsn: str) -> None:
    await directory.migrate()
    try:
        await directory.ping()  # reads every column this build needs
        # Without a column this build reads (before `loom router migrate`), it is unready:
        # each column list is pointed at a missing column, leaving the schema alone.
        for name in ("_CELL", "_ROUTING"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(directory, name, getattr(directory, name).replace("cell,", "no_col,", 1))
                with pytest.raises(directory.DirectoryUnavailable):
                    await directory.ping()
    finally:
        await directory.close()


@pytest.mark.asyncio
async def test_operator_changes_write_the_row_and_its_audit(dsn: str) -> None:
    import asyncpg

    await directory.migrate()
    a, b, q = (f"it-{uuid.uuid4().hex[:8]}" for _ in range(3))
    conn = await asyncpg.connect(dsn)
    try:
        await _cell(conn, a)
        await _cell(conn, b)
        await _cell(conn, q, limit=3)

        async def row(cell: str) -> dict[str, Any]:
            got = await conn.fetchrow("SELECT * FROM loom_router.cells WHERE cell = $1", cell)
            assert got is not None
            return dict(got)

        # The operator's changes: a limit, and a credential rotation with its refusals.
        # Each writes the fields it names and updated_at, and leaves every other column.
        before = await row(b)
        limited = await directory.set_limit(b, closed=True, placements_max=7)
        assert (limited.closed, limited.placements_max) == (True, 7)
        after = await row(b)
        assert after["updated_at"] > before["updated_at"]
        changed = {"closed": True, "placements_max": 7, "updated_at": after["updated_at"]}
        assert after == {**before, **changed}
        cleared = await directory.set_limit(b, placements_max=None)
        assert (cleared.closed, cleared.placements_max) == (True, None)  # closed is kept
        assert await row(b) == {**after, "placements_max": None, "updated_at": ANY}
        with pytest.raises(ValueError):
            await directory.set_limit(b)  # nothing to set: no statement
        with pytest.raises(ValueError):
            await directory.rotate_credential(b, "finsh")  # no such step: nothing written
        with pytest.raises(ValueError):
            await directory.rotate_credential(b, "next")  # no hash to record
        with pytest.raises(ValueError):
            await directory.rotate_credential(b, "finish", "c" * 64)  # a hash finish ignores
        with pytest.raises(directory.NoSuchCell):
            await directory.set_limit(f"nope-{b}", closed=True)
        sha_b = hashlib.sha256(b.encode()).hexdigest()
        with pytest.raises(directory.OperatorRefused, match="records that credential hash"):
            await directory.rotate_credential(a, "next", sha_b)  # b's current hash
        with pytest.raises(directory.OperatorRefused, match="no next credential"):
            await directory.rotate_credential(a, "finish")
        new = hashlib.sha256(f"new-{a}".encode()).hexdigest()
        before = await row(a)
        assert (await directory.rotate_credential(a, "next", new)).credential_next_sha256 == new
        assert await row(a) == {**before, "credential_next_sha256": new, "updated_at": ANY}
        assert (await directory.credentials())[a] == (hashlib.sha256(a.encode()).hexdigest(), new)
        with pytest.raises(directory.OperatorRefused, match="already cell .* next credential"):
            await directory.rotate_credential(a, "next", new)  # this change, retried
        with pytest.raises(directory.OperatorRefused, match="another next credential pending"):
            await directory.rotate_credential(a, "next", "e" * 64)
        with pytest.raises(directory.OperatorRefused, match="records that credential hash"):
            await directory.rotate_credential(b, "next", new)  # a's next hash
        # Two cells asking for one next hash at once: the directory lock lets one in.
        raced = hashlib.sha256(f"raced-{a}".encode()).hexdigest()
        both = await asyncio.gather(
            directory.rotate_credential(b, "next", raced),
            directory.rotate_credential(q, "next", raced),
            return_exceptions=True,
        )
        assert sum(isinstance(r, directory.Cell) for r in both) == 1
        assert sum(isinstance(r, directory.OperatorRefused) for r in both) == 1
        pending = await row(a)
        finished = await directory.rotate_credential(a, "finish")
        assert (finished.credential_sha256, finished.credential_next_sha256) == (new, None)
        assert await row(a) == {
            **pending,
            "credential_sha256": new,
            "credential_next_sha256": None,
            "updated_at": ANY,
        }
        winner = b if isinstance(both[0], directory.Cell) else q
        reverted = await directory.rotate_credential(winner, "revert")
        assert reverted.credential_next_sha256 is None
        assert (await directory.credentials())[winner] == (reverted.credential_sha256,)
        with pytest.raises(directory.OperatorRefused, match="no next credential"):
            await directory.rotate_credential(winner, "revert")

        def steps(cell: str) -> Any:
            return conn.fetch(
                "SELECT action, detail FROM loom_router.audit WHERE actor = 'operator' "
                "AND detail->>'cell' = $1 ORDER BY id",
                cell,
            )

        assert [(r["action"], json.loads(r["detail"])) for r in await steps(a)] == [
            ("credential", {"cell": a, "step": "next", "sha256": new}),
            ("credential", {"cell": a, "step": "finish"}),
        ]
        assert [(r["action"], json.loads(r["detail"])) for r in await steps(b)][:2] == [
            ("limit", {"cell": b, "closed": True, "placements_max": 7}),
            ("limit", {"cell": b, "placements_max": None}),
        ]
        assert [(r["action"], json.loads(r["detail"])) for r in await steps(winner)][-2:] == [
            ("credential", {"cell": winner, "step": "next", "sha256": raced}),
            ("credential", {"cell": winner, "step": "revert"}),
        ]
    finally:
        await conn.close()
        await directory.close()


@pytest.mark.asyncio
async def test_prepare_registers_a_cell_once_and_refuses_what_another_holds(own_dsn: str) -> None:
    import asyncpg

    await directory.migrate()
    first, later, third = (f"rg-{uuid.uuid4().hex[:8]}" for _ in range(3))
    names = (first, later, third)

    def reg(name: str, **over: Any) -> directory.Registration:
        fields: dict[str, Any] = {
            "cell": name,
            "host": f"{name}.example.test",
            "deployment_id": f"dep-{name}",
            "credential_sha256": hashlib.sha256(name.encode()).hexdigest(),
            "bucket": f"docs-{first}",
            "bucket_prefix": f"cells/{name}",
            "probe_url": f"http://{name}.internal:7790",
            "is_first": name == first,
            "upstream_url": "http://upstream.internal:7777" if name == first else "",
            "tenancy_rows": 0,
            "state_rows": 0,
            "prefix_objects": 0,
            "processes_without_cell": 0,
        }
        return directory.Registration(**{**fields, **over})

    conn = await asyncpg.connect(own_dsn)
    try:
        # A later cell before the first is refused; then the first registers.
        # Named before the probe too: a later cell before the first, a first with no upstream.
        assert "registered before any other" in await directory.refusal_before_probe(reg(later))
        no_upstream = reg(first, upstream_url="")
        assert "LOOM_ROUTER_UPSTREAM_URL" in await directory.refusal_before_probe(no_upstream)
        with pytest.raises(directory.OperatorRefused, match="first cell is registered before"):
            await directory.register(reg(later))
        with pytest.raises(directory.OperatorRefused, match="LOOM_ROUTER_UPSTREAM_URL"):
            await directory.register(reg(first, upstream_url=""))
        # Before attach the first cell serves from its prefix's documents and its processes
        # run without LOOM_CELL: neither refuses it.
        row, written = await directory.register(
            reg(first, prefix_objects=2, processes_without_cell=3)
        )
        assert written and (row.is_first, row.state, row.mode) == (True, "attaching", "recording")
        stored = await conn.fetchrow(
            "SELECT is_first, host, state, mode, mode_set_at IS NOT NULL AS mode_set, "
            "credential_sha256, credential_next_sha256, deployment_id, bucket, bucket_prefix, "
            "probe_url, upstream_url, closed, placements_max, placed "
            "FROM loom_router.cells WHERE cell = $1",
            first,
        )
        assert dict(stored) == {
            "is_first": True,
            "host": f"{first}.example.test",
            "state": "attaching",
            "mode": "recording",
            "mode_set": True,
            "credential_sha256": hashlib.sha256(first.encode()).hexdigest(),
            "credential_next_sha256": None,
            "deployment_id": f"dep-{first}",
            "bucket": f"docs-{first}",
            "bucket_prefix": f"cells/{first}",
            "probe_url": f"http://{first}.internal:7790",
            "upstream_url": "http://upstream.internal:7777",
            "closed": False,
            "placements_max": None,
            "placed": 0,
        }
        # The shared host still reaches the deployment the router fronts.
        cells, aliases = await directory.routing()
        table = routing.build(cells, aliases, shared_host="shared.example.test", passthrough="")
        assert table.rule4() == "http://upstream.internal:7777"
        assert (
            await conn.fetchval(
                "SELECT cell FROM loom_router.hosts WHERE host = $1", f"{first}.example.test"
            )
            == first
        )
        # A rerun answers the row and writes nothing; another hash or id is refused.
        again, wrote = await directory.register(reg(first))
        assert (again.cell, wrote) == (first, False)
        # A rerun needs no upstream: only a new row copies it.
        again, wrote = await directory.register(reg(first, upstream_url=""))
        assert (again.cell, wrote) == (first, False)
        with pytest.raises(directory.OperatorRefused, match="not cell"):
            await directory.register(reg(first, credential_sha256="c" * 64))
        # During a rotation the cell's next credential reruns it too.
        await conn.execute(
            "UPDATE loom_router.cells SET credential_next_sha256 = $2 WHERE cell = $1",
            first,
            "d" * 64,
        )
        again, wrote = await directory.register(reg(first, credential_sha256="d" * 64))
        assert (again.cell, wrote) == (first, False)
        await conn.execute(
            "UPDATE loom_router.cells SET credential_next_sha256 = NULL WHERE cell = $1", first
        )
        with pytest.raises(directory.OperatorRefused, match="another deployment id or host"):
            await directory.register(reg(first, deployment_id="dep-other"))
        with pytest.raises(directory.OperatorRefused, match="no longer the shared host"):
            await directory.register(reg(first, is_first=False))
        with pytest.raises(directory.OperatorRefused, match="bucket or prefix"):
            await directory.register(reg(first, bucket_prefix="cells/moved"))
        # A router replica configured with other addresses (a rollout not finished) is
        # refused, and the row keeps the ones registration wrote.
        with pytest.raises(directory.OperatorRefused, match="set this router replica's LOOM"):
            await directory.register(reg(first, probe_url="http://moved.internal:7790"))
        with pytest.raises(directory.OperatorRefused, match="LOOM_ROUTER_UPSTREAM_URL"):
            await directory.register(reg(first, upstream_url="http://moved-up.internal:7777"))
        assert tuple(
            await conn.fetchrow(
                "SELECT probe_url, upstream_url FROM loom_router.cells WHERE cell = $1", first
            )
        ) == (f"http://{first}.internal:7790", "http://upstream.internal:7777")
        # The read before the probe answers exactly what register refuses, and nothing for a
        # new cell or a rerun that passes.
        for over in (
            {"credential_sha256": "c" * 64},
            {"deployment_id": "dep-other"},
            {"is_first": False},
            {"bucket_prefix": "cells/moved"},
            {"probe_url": "http://moved.internal:7790"},
            {"upstream_url": "http://moved-up.internal:7777"},
        ):
            with pytest.raises(directory.OperatorRefused) as refused:
                await directory.register(reg(first, **over))
            assert await directory.refusal_before_probe(reg(first, **over)) == refused.value.reason
        assert await directory.refusal_before_probe(reg(first)) == ""
        assert await directory.refusal_before_probe(reg(later)) == ""
        # A second first cell, and a later cell holding what the first holds, are refused.
        with pytest.raises(directory.OperatorRefused, match="first cell already"):
            await directory.register(reg(later, is_first=True))
        for over, why in (
            ({"credential_sha256": hashlib.sha256(first.encode()).hexdigest()}, "credential"),
            ({"deployment_id": f"dep-{first}"}, "deployment id"),
            ({"host": f"{first}.example.test"}, "names cell"),
            ({"bucket_prefix": f"cells/{first}/sub"}, "shares objects"),
            ({"bucket_prefix": "cells"}, "shares objects"),
            ({"bucket_prefix": ""}, "shares objects"),
        ):
            with pytest.raises(directory.OperatorRefused, match=why):
                await directory.register(reg(later, **over))
        # A new cell's refusals are named before the probe too, as register names them.
        for over in (
            {"credential_sha256": hashlib.sha256(first.encode()).hexdigest()},
            {"deployment_id": f"dep-{first}"},
            {"host": f"{first}.example.test"},
            {"bucket_prefix": f"cells/{first}/sub"},
            {"bucket_prefix": "cells"},
            {"bucket_prefix": ""},
            {"is_first": True},
        ):
            with pytest.raises(directory.OperatorRefused) as refused:
                await directory.register(reg(later, **over))
            assert await directory.refusal_before_probe(reg(later, **over)) == refused.value.reason
        # Stores holding orgs or documents the directory placed nowhere are another cell's;
        # a rerun counts them as its own.
        sibling = f"cells/{first}x"
        with pytest.raises(directory.OperatorRefused, match="tenancy rows"):
            await directory.register(reg(later, bucket_prefix=sibling, tenancy_rows=1))
        # Named before the probe too, as register names it.
        held_rows = reg(later, bucket_prefix=sibling, state_rows=1)
        assert "another cell's state" in await directory.refusal_before_probe(held_rows)
        with pytest.raises(directory.OperatorRefused, match="`first`, mode or own row"):
            await directory.register(reg(later, bucket_prefix=sibling, state_rows=1))
        with pytest.raises(directory.OperatorRefused, match="must hold no object but"):
            await directory.register(reg(later, bucket_prefix=sibling, prefix_objects=1))
        with pytest.raises(directory.OperatorRefused, match="without its LOOM_CELL"):
            await directory.register(reg(later, bucket_prefix=sibling, processes_without_cell=2))
        _, wrote = await directory.register(
            reg(
                first,
                upstream_url="",
                tenancy_rows=4,
                state_rows=3,
                prefix_objects=2,
                processes_without_cell=1,
            )
        )
        assert wrote is False
        # A sibling prefix (not under the first's) and another bucket both register.
        row, written = await directory.register(reg(later, bucket_prefix=f"cells/{first}x"))
        assert written and (row.is_first, row.mode) == (False, "enforced")
        with pytest.raises(directory.OperatorRefused, match="with that --probe-url"):
            await directory.register(
                reg(later, bucket_prefix=f"cells/{first}x", probe_url="http://moved:7790")
            )
        # The shared host moved to a later cell: its rerun is not taken for the first's.
        with pytest.raises(directory.OperatorRefused, match="names cell .* as the first"):
            await directory.register(
                reg(later, bucket_prefix=f"cells/{first}x", is_first=True, probe_url="x")
            )
        _, written = await directory.register(
            reg(third, bucket=f"own-{third}", bucket_prefix="cells/x")
        )
        assert written
        audit = await conn.fetch(
            "SELECT detail FROM loom_router.audit WHERE action = 'register' "
            "AND detail->>'cell' = ANY($1::text[]) ORDER BY id",
            list(names),
        )
        assert [json.loads(a["detail"])["cell"] for a in audit] == [first, later, third]
        assert json.loads(audit[0]["detail"]) == {
            "cell": first,
            "host": f"{first}.example.test",
            "deployment_id": f"dep-{first}",
            "is_first": True,
            "bucket": f"docs-{first}",
            "bucket_prefix": f"cells/{first}",
            "probe_url": f"http://{first}.internal:7790",
            "upstream_url": "http://upstream.internal:7777",
        }
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_directory_never_migrates_into_a_memory_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    monkeypatch.setattr(settings, "router_pg_dsn", memory)
    with pytest.raises(directory.DirectoryUnavailable, match="memory authority"):
        await directory.migrate()
