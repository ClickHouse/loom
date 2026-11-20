"""Real participant rows and Keeper recovery on an opt-in disposable database."""

import contextlib
import os
import sys
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
async def test_participant_ack_is_full_row_and_expiry_is_on_the_server_clock(monkeypatch):
    _live()
    ch = _default_client()
    await ch.connect()
    identity, transition = uuid4(), uuid4()
    monkeypatch.setattr(
        "server.insights.store.provider_participants.process_identity", lambda: identity
    )
    try:
        await record_participant(identity, transition, "running", ch=ch)
        await record_participant(identity, transition, "drained", ch=ch)
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
        with pytest.raises(ValueError, match="state"):
            await record_participant(
                identity, transition, "retired", after=int(rows[0]["version"]), ch=ch
            )
        assert any(str(row["participant_id"]) == str(identity) for row in await participants(ch=ch))
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
async def test_participant_census_excludes_certified_history_before_returning_rows():
    _live()
    from server.core.ch import query_tag, sequential_reads
    from server.core.provider_process import certified, has_certificate
    from server.insights.store import provider_participants as store

    legacy = "test|boot||pidns|7|99||"
    proof = certified(legacy)
    invalid_pid = proof.replace("|7|99|", "|0|99|")
    oversized_pid = proof.replace("|7|99|", "|" + "5" * 5000 + "|99|")
    assert has_certificate(proof)
    assert not has_certificate(invalid_pid)
    assert not has_certificate(oversized_pid)
    blockers = {
        uuid4(): (state, identity)
        for state, identity in (
            ("running", proof),
            ("drained", proof),
            ("retired", ""),
            ("retired", legacy),
            ("retired", legacy + "|unknown"),
            ("retired", proof + "|unknown"),
            ("retired", invalid_pid),
            ("retired", oversized_pid),
        )
    }
    history = {uuid4(): ("retired", proof) for _ in range(256)}
    created = {**history, **blockers}
    expected = {str(identity): value for identity, value in blockers.items()}
    ids = [str(identity) for identity in created]
    ch = _default_client()
    with query_tag("test.provider_participant_census"):
        await ch.connect()
        try:
            await ch.insert(
                store.TABLE,
                [
                    {"participant_id": identity, "state": state, "version": 1, "host_proof": value}
                    for identity, (state, value) in created.items()
                ],
            )
            with sequential_reads():
                raw = await ch.query_all(
                    "SELECT participant_id, state, host_proof FROM ("
                    + store.PARTICIPANTS_SQL
                    + ") WHERE participant_id IN {ids:Array(UUID)}",
                    {"ids": ids},
                )
            assert {
                str(row["participant_id"]): (row["state"], row["host_proof"]) for row in raw
            } == expected, "ClickHouse must exclude certified history before returning census rows"
            kept = await store.participants(ch=ch)
            assert {
                str(row["participant_id"]): (row["state"], row["host_proof"])
                for row in kept
                if str(row["participant_id"]) in ids
            } == expected
        finally:
            with (
                contextlib.suppress(BaseException) if sys.exception() else contextlib.nullcontext()
            ):
                try:
                    await ch.execute(
                        f"ALTER TABLE {store.TABLE} DELETE WHERE participant_id IN {{ids:Array(UUID)}} "
                        "SETTINGS mutations_sync=2",
                        {"ids": ids},
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
