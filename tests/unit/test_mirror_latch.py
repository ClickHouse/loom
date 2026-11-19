"""A negative deployment decision needs fresh, same-backend ordered evidence."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.core import request_work
from server.core.ch import current_query_tag, query_tag, statement_settings
from server.knowledge.store import mirror_latch as store
from tests.unit.clock_fake import ManualClock
from tests.unit.mirror_latch_fake import A, B, KeeperWorld


@pytest.mark.parametrize(
    "operation, stage, method",
    [
        (store.enabled, "enabled.engines", "query_one"),
        (store.ensure, "ensure.set", "execute"),
    ],
)
async def test_refusal_logs_only_stage_exception_type_and_numeric_code(
    operation, stage, method, caplog, monkeypatch
):
    world = KeeperWorld()
    error = RuntimeError("Code: 999. private-body SELECT secret https://example.invalid/token")
    monkeypatch.setattr(world, method, AsyncMock(side_effect=error))
    with (
        caplog.at_level("WARNING", logger=store.__name__),
        pytest.raises(
            store.LatchUnavailable, match="^Session protection state could not be confirmed$"
        ),
    ):
        await operation(ch=world)

    records = [record for record in caplog.records if record.name == store.__name__]
    assert len(records) == 1
    assert records[0].args[:3] == (stage, "RuntimeError", 999)
    _timing_fields(records[0])
    assert records[0].exc_info is None and records[0].stack_info is None
    assert "private-body" not in caplog.text and "example.invalid" not in caplog.text
    assert not world.leases


async def test_nonce_mismatch_logs_its_stage_and_keeps_the_refusal_and_cleanup(caplog):
    world = KeeperWorld()
    world.fault = "expired_nonce"
    with caplog.at_level("WARNING", logger=store.__name__), pytest.raises(store.LatchUnavailable):
        await store.enabled(ch=world)

    records = [record for record in caplog.records if record.name == store.__name__]
    assert len(records) == 1
    assert records[0].args[:3] == ("enabled.nonce_confirm", "LatchUnavailable", None)
    _timing_fields(records[0])
    assert not world.leases and world.counts["release"] == 1


def _timing_fields(record):
    assert len(record.args) == 7
    elapsed, current, expired, completed = record.args[3:]
    assert type(elapsed) is int and elapsed >= 0
    assert type(current) is int and 0 <= current <= elapsed
    assert type(expired) is bool
    assert isinstance(completed, dict)
    assert all(type(value) is int and 0 <= value <= elapsed for value in completed.values())
    return elapsed, current, expired, completed


@pytest.mark.parametrize("failure", ["release", "verify"])
async def test_nonce_cleanup_failure_names_the_exact_statement(failure, caplog, monkeypatch):
    clock = ManualClock(t=1000)
    monkeypatch.setattr(store, "monotonic", clock.monotonic)
    world = KeeperWorld()
    execute, query_one = world.execute, world.query_one

    async def release(sql, params=None):
        if sql == store.LOCK_RELEASE and failure == "release" and not world.counts["release"]:
            world.route("release")
            clock.advance(0.375)
            raise TimeoutError("private-body SELECT secret https://example.invalid/token")
        return await execute(sql, params)

    async def verify(sql, params=None):
        if sql == store._NONCE and failure == "verify" and world.counts["release"]:
            clock.advance(0.375)
            raise TimeoutError("private-body SELECT secret https://example.invalid/token")
        return await query_one(sql, params)

    monkeypatch.setattr(world, "execute", release)
    monkeypatch.setattr(world, "query_one", verify)
    with caplog.at_level("WARNING", logger=store.__name__), pytest.raises(store.LatchUnavailable):
        await store.enabled(ch=world)

    records = [record for record in caplog.records if record.name == store.__name__]
    assert len(records) == 1
    assert records[0].args[:3] == (f"enabled.nonce_{failure}", "TimeoutError", None)
    elapsed, current, expired, completed = _timing_fields(records[0])
    assert (elapsed, current, expired) == (375, 375, False)
    assert completed["enabled.nonce_confirm"] == 0
    assert ("enabled.nonce_release" in completed) is (failure == "verify")
    assert records[0].exc_info is None and records[0].stack_info is None
    assert "private-body" not in caplog.text and "example.invalid" not in caplog.text
    assert not world.leases and world.counts["release"] == 2 and world.counts["nonce"] == 1


@pytest.mark.parametrize("outer_expires", [False, True])
async def test_timeout_warning_records_phase_elapsed_and_actual_outer_expiry(
    outer_expires, caplog, monkeypatch
):
    clock = ManualClock(t=1000)
    monkeypatch.setattr(store, "monotonic", clock.monotonic, raising=False)
    deadlines = []

    def timeout(seconds):
        assert seconds == 5
        deadline = asyncio.timeout(seconds)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(store, "asyncio", SimpleNamespace(timeout=timeout))

    class TimedWorld(KeeperWorld):
        @asynccontextmanager
        async def coordination_session(self):
            async with super().coordination_session():
                clock.advance(1)
                yield

        async def query_one(self, sql, params=None):
            if sql == store._LATCH:
                clock.advance(0.375)
                if outer_expires:
                    deadlines[0].reschedule(asyncio.get_running_loop().time() - 1)
                    await asyncio.sleep(0)
                    pytest.fail("the outer deadline must cancel the proof")
                raise TimeoutError("private-body SELECT secret https://example.invalid/token")
            clock.advance(0.125)
            return await super().query_one(sql, params)

    world = TimedWorld()
    with caplog.at_level("WARNING", logger=store.__name__), pytest.raises(store.LatchUnavailable):
        await store.enabled(ch=world)

    records = [record for record in caplog.records if record.name == store.__name__]
    assert len(records) == 1
    assert records[0].args[:3] == ("enabled.latch", "TimeoutError", None)
    assert _timing_fields(records[0]) == (
        1500,
        375,
        outer_expires,
        {"enabled.coordination": 1000, "enabled.engines": 1125},
    )
    assert deadlines[0].expired() is outer_expires
    assert records[0].exc_info is None and records[0].stack_info is None
    assert "private-body" not in caplog.text and "example.invalid" not in caplog.text
    assert world.calls == ["engines"] and not world.leases


async def test_positive_latch_close_timeout_has_its_own_phase(caplog, monkeypatch):
    clock = ManualClock(t=1000)
    monkeypatch.setattr(store, "monotonic", clock.monotonic, raising=False)

    class ClosingWorld(KeeperWorld):
        @asynccontextmanager
        async def coordination_session(self):
            async with super().coordination_session():
                clock.advance(0.25)
                yield
                clock.advance(1.5)
                raise TimeoutError("private close details")

        async def query_one(self, sql, params=None):
            clock.advance(0.375 if sql == store._LATCH else 0.125)
            return await super().query_one(sql, params)

    world = ClosingWorld(enabled=True, visible=True)
    with caplog.at_level("WARNING", logger=store.__name__), pytest.raises(store.LatchUnavailable):
        await store.enabled(ch=world)

    records = [record for record in caplog.records if record.name == store.__name__]
    assert len(records) == 1
    assert records[0].args[:3] == ("enabled.close", "TimeoutError", None)
    assert _timing_fields(records[0]) == (
        2250,
        1500,
        False,
        {"enabled.coordination": 250, "enabled.engines": 375, "enabled.latch": 750},
    )
    assert world.calls == ["engines", "latch"] and not world.leases
    assert "private close details" not in caplog.text


async def test_diagnostic_logger_failure_cannot_replace_the_privacy_refusal(monkeypatch):
    world = KeeperWorld()
    monkeypatch.setattr(world, "query_one", AsyncMock(side_effect=TimeoutError("private timeout")))

    def broken_logger(*args, **kwargs):
        raise RuntimeError("private logging failure")

    monkeypatch.setattr(store.log, "warning", broken_logger)
    with pytest.raises(
        store.LatchUnavailable, match="^Session protection state could not be confirmed$"
    ):
        await store.enabled(ch=world)
    assert not world.leases


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
