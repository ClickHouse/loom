"""Prepare one private cell's real stores before its HTTPS process starts."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from server.auth import keyring, revocation
from server.core import cell, pg, pg_profile
from server.core.ch import get_client, set_query_tag
from server.core.schema import pg_migrate
from server.settings import settings


async def seed(result_file: Path, org: str) -> None:
    ch = get_client()
    await ch.connect()
    try:
        set_query_tag("test.cell_each.seed")
        await pg_migrate.migrate()
        await pg_profile.require_authority(ch)
        deployment = await pg_profile.deployment_id()
        fingerprint = cell.as_value(cell.fingerprint(deployment))
        # These databases have no serving process yet. The rows are the prepared
        # fixture state; actual automation metadata is written through the CLI.
        await pg.execute("INSERT INTO loom.tenancy (org) VALUES ($1)", org)
        await pg.execute(
            "INSERT INTO loom.cell_first (is_first, cell) VALUES ($1, $2)",
            settings.cell == "alpha",
            settings.cell,
        )
        await pg.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('enforced')")
        await pg.execute("INSERT INTO loom.cell_self (current_fp) VALUES ($1)", fingerprint)
        await keyring.record_root(ch=ch)
        assert await keyring.refresh(ch=ch)
        await revocation.refresh()
        await asyncio.to_thread(
            result_file.write_text,
            json.dumps({"deployment_id": deployment, "fingerprint": fingerprint}),
        )
    finally:
        await pg.close_pool()
        await ch.disconnect()


if __name__ == "__main__":
    asyncio.run(seed(Path(sys.argv[1]), sys.argv[2]))
