"""Real participant rows and Keeper recovery on an opt-in disposable database."""

import os
from uuid import uuid4

import pytest

from server.core.provider_process import process_identity, stopped_process
from server.core.repo_base import _default_client
from server.insights.store.provider_participants import (
    participants,
    record_participant,
    storage_idle,
)


@pytest.mark.asyncio
async def test_participant_ack_is_full_row_and_retirement_is_explicit():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    ch = _default_client()
    await ch.connect()
    identity, transition = uuid4(), uuid4()
    try:
        await record_participant(identity, transition, "running", ch=ch)
        await record_participant(identity, transition, "drained", ch=ch)
        rows = [
            row for row in await participants(ch=ch) if str(row["participant_id"]) == str(identity)
        ]
        assert len(rows) == 1
        assert rows[0]["state"] == "drained"
        assert str(rows[0]["transition_id"]) == str(transition)
        await record_participant(
            identity, transition, "retired", after=int(rows[0]["version"]), ch=ch
        )
        assert all(str(row["participant_id"]) != str(identity) for row in await participants(ch=ch))
        assert await storage_idle(ch=ch)
    finally:
        try:
            await ch.execute(
                "ALTER TABLE loom._provider_participants DELETE WHERE participant_id={id:UUID} "
                "SETTINGS mutations_sync=2",
                {"id": str(identity)},
            )
        finally:
            await ch.disconnect()


def test_keeper_never_steals_from_a_live_process_even_with_expired_timestamp():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    from server.core.schema.migrate import client
    from server.core.schema.migrate_lease import LeaseConfig
    from server.insights.store.provider_lock import ProcessKeeperLock

    ch = client()
    name = f"provider-test-{uuid4()}"
    owner = ProcessKeeperLock(ch, holder=f"{process_identity()}/{uuid4()}", lock_name=name)
    contender = ProcessKeeperLock(
        ch, holder=f"{process_identity()}/{uuid4()}", lock_name=name, cfg=LeaseConfig(wait_s=0)
    )
    try:
        assert owner.acquire() == "held"
        ch.command(
            "ALTER TABLE loom._migration_lock UPDATE renewed_at=toDateTime64('2000-01-01',3) "
            "WHERE name={name:String} SETTINGS keeper_map_strict_mode=1",
            parameters={"name": name},
        )
        assert contender.acquire() == "timeout"
        with stopped_process(str(process_identity())) as stopped:
            assert not stopped
        assert owner._holder_row()[0] == owner.holder
    finally:
        owner.release()
        contender.release()
        ch.close()
