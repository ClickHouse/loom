"""The cost of a cell's org triggers (server/core/cell_triggers.py) on writes to a guarded
table, in a database of its own on the Postgres LOOM_PG_DSN names, on a cell in
`enforced` with the written org held: single-row inserts from a marked session (Loom's
own) and an unmarked one, triggers off and on, and bulk COPYs, marked with the triggers
on and off in turn, and unmarked. Needs no provider and no server.

    PYTHONPATH=$PWD LOOM_PG_DSN=postgresql://... .venv/bin/python bench/cell_trigger_cost.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg

from server.core import cell_triggers
from server.core.schema import pg_migrate
from tests.integration._pg_database import create_database, drop_database

INSERT = "INSERT INTO loom.scope_generations (org, scope, generation) VALUES ($1, $2, 1)"
RUNS, CALLS = 5, 2000
_ENABLED = (
    "SELECT tgenabled::text AS e FROM pg_trigger "
    "WHERE tgrelid = 'loom.scope_generations'::regclass AND tgname = ANY($1::text[])"
)
BULK_ROWS = 500_000
BULK_ROUNDS = 6


async def _single_rows(conn: Any) -> tuple[float, float]:
    """p50 and p99 in ms of CALLS single-row inserts of a held org."""
    lat = []
    for _ in range(CALLS):
        t = time.perf_counter()
        await conn.execute(INSERT, "held", f"s-{uuid.uuid4().hex}")
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    return lat[len(lat) // 2], lat[int(len(lat) * 0.99)]


async def _copy(conn: Any, data: bytes) -> float:
    await conn.execute("TRUNCATE loom.scope_generations")
    t = time.perf_counter()
    await conn.copy_to_table(
        "scope_generations",
        schema_name="loom",
        source=io.BytesIO(data),
        columns=["org", "scope", "generation"],
    )
    return time.perf_counter() - t


async def _triggers(conn: Any, on: bool) -> None:
    """Triggers on (`ensure`) or off, and the treatment checked: `ensure` logs and skips a
    table it cannot change, so a run that claims them on proves they are."""
    if on:
        # ensure skips a table whose lock it cannot take in 100 ms (an autovacuum after a
        # bulk load holds one), as a boot does: try again until the triggers are there.
        for _ in range(100):
            await cell_triggers.ensure(conn)
            if len(await conn.fetch(_ENABLED, list(cell_triggers.ORG_TRIGGERS))) == 2:
                break
            await asyncio.sleep(0.2)
    else:
        for name in cell_triggers.ORG_TRIGGERS:
            await conn.execute(f"DROP TRIGGER IF EXISTS {name} ON loom.scope_generations")
    enabled = await conn.fetch(_ENABLED, list(cell_triggers.ORG_TRIGGERS))
    assert [r["e"] for r in enabled] == (["O", "O"] if on else []), enabled


async def main() -> None:
    admin = os.environ["LOOM_PG_DSN"]
    name, dsn = await create_database(admin)
    try:
        marked = await asyncpg.connect(dsn, server_settings={"loom.tenancy_checked": "on"})
        unmarked = await asyncpg.connect(dsn)

        @contextlib.asynccontextmanager
        async def _session() -> AsyncIterator[Any]:
            yield marked

        await pg_migrate.migrate(connect=_session)
        await marked.execute("INSERT INTO loom.cell_first (is_first, cell) VALUES (false, 'eu-2')")
        await marked.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('enforced')")
        await marked.execute("INSERT INTO loom.cell_self (current_fp) VALUES ('{\"v\": 1}'::jsonb)")
        await marked.execute("INSERT INTO loom.tenancy (org) VALUES ('held')")
        # Off and on alternate, so neither gets the warm-up.
        single: dict[tuple[str, bool], list[tuple[float, float]]] = {}
        for run in range(RUNS):
            for on in (run % 2 == 1, run % 2 == 0):
                await _triggers(marked, on)
                for label, conn in (("marked", marked), ("unmarked", unmarked)):
                    single.setdefault((label, on), []).append(await _single_rows(conn))
        for (label, on), runs in sorted(single.items()):
            p99s = [r[1] for r in runs]
            print(
                f"{label} single-row insert, triggers {'on' if on else 'off'}: "
                f"p50 {statistics.median(r[0] for r in runs):.3f} ms, p99 median "
                f"{statistics.median(p99s):.3f} ms (runs {min(p99s):.3f} to {max(p99s):.3f})"
            )
        data = "".join(f"held\tc-{i}\t1\n" for i in range(BULK_ROWS)).encode()
        # Each round times both paths with the triggers off and on, in an order that
        # rotates, and the cost is the paired difference within a round: the host's drift
        # between rounds cancels.
        paired: dict[str, list[float]] = {"marked": [], "unmarked": []}
        for round_ in range(BULK_ROUNDS):
            order = [("marked", marked), ("unmarked", unmarked)]
            if round_ % 2:
                order.reverse()
            for label, conn in order:
                timed = {}
                for on in (round_ % 2 == 0, round_ % 2 == 1):
                    await _triggers(marked, on)
                    timed[on] = await _copy(conn, data)
                paired[label].append(timed[True] - timed[False])
        for label, diffs in paired.items():
            per_row = [d / BULK_ROWS * 1e6 for d in diffs]
            print(
                f"{label} {BULK_ROWS:,}-row COPY, on minus off: median {statistics.median(per_row):.3f} "
                f"us a row (rounds {min(per_row):.3f} to {max(per_row):.3f}, {len(diffs)} rounds)"
            )
        await unmarked.close()
        await marked.close()
    finally:
        await drop_database(admin, name)


if __name__ == "__main__":
    asyncio.run(main())
