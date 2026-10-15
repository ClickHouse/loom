"""Real participant rows and Keeper recovery on an opt-in disposable database."""

import os
from uuid import uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.store.provider_participants import (
    participants,
    record_participant,
    running_queries,
)


def _live() -> None:
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")


@pytest.mark.asyncio
async def test_participant_ack_is_full_row_and_expiry_is_on_the_server_clock():
    _live()
    ch = _default_client()
    await ch.connect()
    identity, transition = uuid4(), uuid4()
    try:
        await record_participant(identity, transition, "running", stale_after_s=60, ch=ch)
        await record_participant(identity, transition, "drained", stale_after_s=60, ch=ch)
        rows = [r for r in await participants(ch=ch) if str(r["participant_id"]) == str(identity)]
        assert len(rows) == 1
        assert rows[0]["state"] == "drained" and not rows[0]["expired"]
        assert str(rows[0]["transition_id"]) == str(transition)
        await ch.execute(
            "ALTER TABLE loom._provider_participants UPDATE heartbeat_at = now64(3) - 120 "
            "WHERE participant_id = {id:UUID} SETTINGS mutations_sync = 2",
            {"id": str(identity)},
        )
        rows = [r for r in await participants(ch=ch) if str(r["participant_id"]) == str(identity)]
        assert rows[0]["expired"]
        assert await running_queries(identity, ch=ch) == 0
        await record_participant(
            identity, transition, "retired", after=int(rows[0]["version"]), ch=ch
        )
        assert all(str(row["participant_id"]) != str(identity) for row in await participants(ch=ch))
    finally:
        try:
            await ch.execute(
                "ALTER TABLE loom._provider_participants DELETE WHERE participant_id={id:UUID} "
                "SETTINGS mutations_sync=2",
                {"id": str(identity)},
            )
        finally:
            await ch.disconnect()


@pytest.mark.asyncio
async def test_keeper_never_steals_from_a_beating_process_even_with_expired_timestamp():
    _live()
    from server.core import leases
    from server.insights.store.provider_lock import holder_stopped

    ch = _default_client()
    await ch.connect()
    name = f"provider-test-{uuid4()}"
    owner_id = uuid4()
    await ch.insert(
        "loom._provider_participants",
        [
            {
                "participant_id": owner_id,
                "transition_id": uuid4(),
                "state": "running",
                "version": 1,
                "stale_after_s": 60,
            }
        ],
    )
    owner = await leases.take(name, ttl_s=60, holder=f"{owner_id}/{uuid4()}", ch=ch)
    assert owner is not None
    contender: leases.Lease | None = None
    try:
        await ch.execute(
            "ALTER TABLE loom._migration_lock UPDATE renewed_at=toDateTime64('2000-01-01',3) "
            "WHERE name={name:String} SETTINGS keeper_map_strict_mode=1",
            {"name": name},
        )
        assert await leases.take(name, ttl_s=60, may_take_over=holder_stopped, ch=ch) is None
        # The owner's heartbeat expires: the contender takes the lock over.
        await ch.execute(
            "ALTER TABLE loom._provider_participants UPDATE heartbeat_at = now64(3) - 120 "
            "WHERE participant_id = {id:UUID} SETTINGS mutations_sync = 2",
            {"id": str(owner_id)},
        )
        contender = await leases.take(name, ttl_s=60, may_take_over=holder_stopped, ch=ch)
        assert contender is not None and contender.held
    finally:
        if contender is not None:
            await contender.release()
        await owner.release()
        await ch.execute(
            "ALTER TABLE loom._provider_participants DELETE WHERE participant_id={id:UUID} "
            "SETTINGS mutations_sync=2",
            {"id": str(owner_id)},
        )
        await ch.disconnect()
