"""A negative deployment decision needs fresh, same-backend ordered evidence."""

from __future__ import annotations

import asyncio

import pytest

from server.core import request_work
from server.core.ch import current_query_tag, query_tag, statement_settings
from server.knowledge.store import mirror_latch as store
from tests.unit.mirror_latch_fake import A, B, KeeperWorld


async def test_an_absent_latch_is_never_a_cached_negative_decision():
    world = KeeperWorld()
    assert not await store.enabled(ch=world)
    assert not world.leases and world.counts["barrier"] == 1
    # Another process enables after the first decision; this follower has not seen it.
    world.enabled = True
    assert await store.enabled(ch=world)
    assert not world.leases and world.counts["barrier"] == 2


async def test_known_positive_latch_needs_no_barrier_or_cleanup():
    world = KeeperWorld(enabled=True, visible=True)
    assert await store.enabled(ch=world)
    assert world.calls == ["engines", "latch"] and not world.leases


@pytest.mark.parametrize(
    "stage, occurrence", [("latch", 1), ("latch", 2), ("keeper", 2), ("nonce", 1), ("nonce", 2)]
)
async def test_every_read_attests_its_backend_even_when_the_latch_or_nonce_is_empty(
    stage, occurrence
):
    world = KeeperWorld()
    world.routes[stage, occurrence] = B
    with pytest.raises(
        store.LatchUnavailable, match="^Session protection state could not be confirmed$"
    ):
        await store.enabled(ch=world)
    assert not world.leases


async def test_write_on_another_bootstrap_backend_cannot_pass_an_a_b_a_read_sequence():
    world = KeeperWorld(enabled=True)
    world.routes["barrier", 1] = B
    with pytest.raises(store.LatchUnavailable):
        await store.enabled(ch=world)
    assert not world.visible[A] and world.visible[B]
    assert world.counts["nonce"] == 1 and not world.leases


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_engine",
        "wrong_ensemble",
        "wrong_primary_key",
        "missing_keeper",
        "expired_keeper",
        "keeper_replaced",
        "expired_nonce",
        "latch_backend_error",
        "ambiguous_write",
    ],
)
async def test_uncertain_or_changed_evidence_never_authorizes_an_absence(fault):
    world = KeeperWorld()
    world.fault = fault
    with pytest.raises(
        store.LatchUnavailable, match="^Session protection state could not be confirmed$"
    ):
        await store.enabled(ch=world)
    assert not world.leases


async def test_a_duplicate_barrier_is_not_accepted_and_foreign_ownership_is_preserved():
    world = KeeperWorld()
    world.fault = "foreign_duplicate"
    with pytest.raises(store.LatchUnavailable):
        await store.enabled(ch=world)
    assert len(world.leases) == 1
    assert next(iter(world.leases.values()))[0] == "foreign-holder"
    assert world.counts["nonce"] == 0


async def test_cleanup_failure_cannot_turn_an_unlatched_read_into_admission():
    world = KeeperWorld()
    world.fault = "cleanup_error"
    with pytest.raises(store.LatchUnavailable):
        await store.enabled(ch=world)
    assert len(world.leases) == 1 and world.counts["release"] == 2


async def test_cancellation_after_commit_cleans_only_the_own_key_and_stays_cancellation():
    world = KeeperWorld()
    world.fault = "cancelled_write"
    with pytest.raises(asyncio.CancelledError):
        await store.enabled(ch=world)
    assert not world.leases and world.counts["release"] == 1


@pytest.mark.parametrize("racing", [False, True])
async def test_positive_enablement_is_confirmed_without_a_negative_barrier(racing):
    world = KeeperWorld()
    world.fault = "racing_set" if racing else ""
    await store.ensure(ch=world)
    assert world.enabled and world.visible[A]
    assert world.counts["set"] == 1 and not world.counts["barrier"]
    await store.ensure(ch=world)
    assert world.counts["set"] == 2


async def test_enablement_after_an_erasure_does_not_trust_a_followers_stale_positive():
    world = KeeperWorld(enabled=False, visible=True)
    await store.ensure(ch=world)
    assert world.enabled and world.counts["set"] == 1


@pytest.mark.parametrize("operation", [store.enabled, store.ensure])
@pytest.mark.parametrize("outcome", ["success", "refusal", "cancelled"])
async def test_admission_restores_the_callers_query_tag_after_every_exit(operation, outcome):
    class CancelledWorld(KeeperWorld):
        async def query_one(self, sql, params=None):
            raise asyncio.CancelledError()

    world = CancelledWorld() if outcome == "cancelled" else KeeperWorld()
    if outcome == "refusal":
        world.fault = "wrong_engine"
    with request_work.http_owner({}), query_tag("memory.search caller-request-id"):
        request_work.begin()
        if outcome == "success":
            await operation(ch=world)
        else:
            error = asyncio.CancelledError if outcome == "cancelled" else store.LatchUnavailable
            with pytest.raises(error):
                await operation(ch=world)
        assert current_query_tag() == "memory.search caller-request-id"
        following = statement_settings("SELECT 42")
        assert following["log_comment"] == "memory.search caller-request-id"
        assert following["query_id"].startswith("caller-request-id:")


async def test_positive_repair_refuses_a_write_routed_away_from_the_validated_store():
    world = KeeperWorld(enabled=False, visible=True)
    # Both metadata reads and the stale positive stay on A; only SET reaches drifted B.
    world.routes["set", 1] = B
    world.detached_stores.add(B)
    with pytest.raises(store.LatchUnavailable):
        await store.ensure(ch=world)
    assert not world.enabled
