"""Physical privacy state is proved in a child with its own control-plane database."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from server.core.ch import CHClient, caller_shaped
from server.core.ch_target import ch_secure
from server.core.paths import MIGRATIONS_DIR
from server.core.schema.migrate_lease import LOCK_DDL
from server.knowledge.store import mirror_latch
from server.settings import settings
from tests.integration._redact import redact

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.parametrize("mode", ["proof", "missing"])
def test_physical_session_latch(ch_client, mode):
    # Only this exact database and its qualified Keeper paths are disposable.
    # The child also owns all metadata probes; none reach the parent's caches.
    database = "it_session_latch_" + uuid.uuid4().hex
    try:
        ch_client.command("CREATE DATABASE {db:Identifier}", parameters={"db": database})
        env = {
            **os.environ,
            "LOOM_ENV_FILE": "",
            "LOOM_CH_DATABASE": database,
            "LOOM_CH_TELEMETRY_DATABASE": database + "_telemetry",
            "LOOM_CH_HOST": str(settings.ch_host),
            "LOOM_CH_PORT": str(settings.ch_port),
            "LOOM_CH_USER": str(settings.ch_user),
            "LOOM_CH_PASSWORD": str(settings.ch_password),
            "LOOM_CH_SECURE": str(ch_secure()).lower(),
        }
        result = subprocess.run(
            [sys.executable, "-m", __name__, mode],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, redact(result.stdout + result.stderr)
    finally:
        ch_client.command(
            "DROP DATABASE IF EXISTS {db:Identifier} SYNC", parameters={"db": database}
        )


async def _proof(client):
    assert not await mirror_latch.enabled(ch=client)
    assert not await mirror_latch.enabled(ch=client)
    assert await client.query_one("SELECT count() AS n FROM loom._migration_lock") == {"n": 0}
    with caller_shaped():
        async with client.coordination_session():
            row = await client.query_one(
                "SELECT currentUser() AS principal, getSetting('use_query_cache') AS cache, "
                "getSetting('use_query_condition_cache') AS condition_cache"
            )
    assert row == {"principal": settings.ch_user, "cache": 0, "condition_cache": 0}
    with pytest.raises(DatabaseError, match="Session protection writer changed"):
        await client.execute(mirror_latch._SET, {"backend": str(uuid.UUID(int=0))})
    assert not await mirror_latch.enabled(ch=client)
    await mirror_latch.ensure(ch=client)
    assert await mirror_latch.enabled(ch=client)
    await mirror_latch.ensure(ch=client)
    assert await client.query_one("SELECT count() AS n FROM loom.knowledge_mirror_latch") == {
        "n": 1
    }
    assert await client.query_one("SELECT count() AS n FROM loom._migration_lock") == {"n": 0}


async def _native(mode):
    client = CHClient()
    try:
        await client.connect()
        await client.execute(LOCK_DDL)
        migration = MIGRATIONS_DIR / "108_knowledge_mirror_latch.sql"
        await client.execute(migration.read_text(encoding="utf-8"))
        if mode == "proof":
            await _proof(client)
        else:
            assert mode == "missing"
            await client.execute("DROP TABLE loom.knowledge_mirror_latch SYNC")
            for operation in (mirror_latch.enabled, mirror_latch.ensure):
                with pytest.raises(
                    mirror_latch.LatchUnavailable,
                    match="^Session protection state could not be confirmed$",
                ):
                    await operation(ch=client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(_native(sys.argv[1]))
