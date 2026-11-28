"""The native command lifetime preserves its first failure through owned cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.cli import _ch
from server.core import background, retirement
from server.core.model_work_gate import ModelWorkGate
from server.insights.app import provider_barrier as barrier
from server.insights.app import provider_command as command


@pytest.fixture
def native(monkeypatch):
    from server import boot
    from server.core import ch, pg, pg_profile
    from server.memory.app import embedder_calibration
    from server.settings import settings

    events = []
    gate = retirement.get_gate()
    client = SimpleNamespace(connected=False)
    errors = {}
    terminal_ends = []
    reversible_closes = []
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    model_gate = ModelWorkGate()

    def event(name):
        events.append(name)
        if name in errors:
            raise errors[name]

    async def connect():
        event("connect")
        client.connected = True

    async def close_ch():
        client.connected = False
        event("ch")

    async def close_pg():
        assert not (gate.role == "command" and gate.closed)
        reversible_closes.append(True)
        event("pg")

    async def close_pg_until(*, end):
        assert gate.pg_sealed and gate.quiescent
        assert end > asyncio.get_running_loop().time()
        terminal_ends.append(end)
        assert terminal_ends == [terminal_ends[0]] * len(terminal_ends)
        event("pg")
        return True

    async def authority(sql, params=None):
        assert sql == pg_profile._CH_RECORD
        return {"authority": "clickhouse", "deployment_id": str(UUID(int=1)), "epoch": 1}

    async def verify(_):
        assert not gate.enrolled
        event("verify")

    async def enroll():
        event("enroll")
        gate.enrolled = True

    async def acceptance(_):
        assert gate.enrolled and gate.active and not gate.closed
        event("acceptance")

    async def configuration():
        event("configuration")

    async def flush():
        event("flush")

    async def certificate(proof, *, ch):
        assert ch is client
        assert gate.quiescent and gate.pg_sealed and gate.enrolled and client.connected
        assert proof.version > proof.after
        assert events[-1] == "pg"
        assert terminal_ends and not reversible_closes
        event("certificate")

    client.connect, client.disconnect, client.query_one = connect, close_ch, authority
    monkeypatch.setattr(ch, "get_client", lambda: client)
    monkeypatch.setattr(command, "get_client", lambda: client, raising=False)
    monkeypatch.setattr(pg, "close_pool", close_pg)
    monkeypatch.setattr(pg, "close_pool_until", close_pg_until)
    monkeypatch.setattr(pg_profile, "check_configured", verify)
    monkeypatch.setattr(_ch, "check_orgs", acceptance)
    monkeypatch.setattr(command, "register_process", enroll)
    monkeypatch.setattr(command, "load_active_configuration", configuration)
    monkeypatch.setattr(command, "_adopt_overrides", AsyncMock(return_value=True))
    monkeypatch.setattr(command, "get_model_work_gate", lambda: model_gate)
    monkeypatch.setattr(barrier, "get_model_work_gate", lambda: model_gate)
    monkeypatch.setattr(background, "_registered_tasks", set())
    monkeypatch.setattr(background, "_buffer_drainers", {})
    monkeypatch.setattr(boot, "drain_writes", flush)
    monkeypatch.setattr(barrier, "_OWN", barrier._Own())
    monkeypatch.setattr(
        barrier, "_HEARTBEAT", SimpleNamespace(signal_stop=lambda: None, running=False)
    )
    monkeypatch.setattr(barrier, "_server_side_settled", AsyncMock(return_value=True))
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=0))
    monkeypatch.setattr(barrier, "record_retirement", certificate)
    monkeypatch.setattr(embedder_calibration, "stop_refresh_loop", lambda: event("stop"))
    return SimpleNamespace(
        events=events,
        errors=errors,
        client=client,
        gate=gate,
        model_gate=model_gate,
        event=event,
        terminal_ends=terminal_ends,
        reversible_closes=reversible_closes,
    )


FIRST = [ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]


@pytest.mark.parametrize("first_class", FIRST)
@pytest.mark.parametrize("entry", ["verify", "acceptance", "configuration", "work"])
@pytest.mark.parametrize("secondary", ["pg", "ch", "certificate"])
def test_native_first_entry_or_work_error_survives_all_close_boundaries(
    native, first_class, entry, secondary
):
    first = first_class("first")
    native.errors[entry] = first
    native.errors[secondary] = OSError("secondary")

    async def work(_):
        native.event("work")
        return 7

    with pytest.raises(first_class) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first
    if entry == "verify":
        assert native.events[-2:] == ["pg", "ch"]
        assert native.reversible_closes == [True] and not native.terminal_ends
    else:
        assert not native.reversible_closes and len(native.terminal_ends) == 1
        if secondary == "pg":
            assert native.events[-2:] == ["pg", "ch"]
            assert "certificate" not in native.events
        else:
            assert native.events[-3:] == ["pg", "certificate", "ch"]
    assert native.events.count("pg") == native.events.count("ch") == 1
    assert not native.client.connected
    assert native.model_gate.active == native.gate.active == 0
    if entry != "work":
        assert "work" not in native.events
    if entry != "verify":
        assert "stop" in native.events


@pytest.mark.parametrize("first_class", FIRST)
@pytest.mark.parametrize("connector", ["connect_checked", "connect_verified"])
@pytest.mark.asyncio
async def test_connect_entry_preserves_refusal_and_attempts_both_closes(
    native, first_class, connector
):
    first = first_class("pairing refusal")
    native.errors.update(verify=first, pg=OSError("pg"), ch=RuntimeError("ch"))
    call = getattr(_ch, connector)
    with pytest.raises(first_class) as caught:
        if connector == "connect_checked":
            await call(native.client, orgs=("o",))
        else:
            await call(native.client)
    assert caught.value is first
    assert native.events == ["connect", "verify", "pg", "ch"]


@pytest.mark.parametrize("first_class", FIRST)
@pytest.mark.asyncio
async def test_disconnect_preserves_first_pg_failure_and_still_closes_ch(native, first_class):
    first = first_class("pg close")
    native.errors.update(pg=first, ch=OSError("ch close"))
    with pytest.raises(first_class) as caught:
        await _ch.disconnect(native.client)
    assert caught.value is first
    assert native.events == ["pg", "ch"]


@pytest.mark.parametrize("cleanup", ["stop", "flush", "certificate", "pg", "ch"])
@pytest.mark.parametrize("first_class", FIRST)
def test_required_native_cleanup_error_after_success_is_fatal(native, cleanup, first_class):
    first = first_class(cleanup)
    native.errors[cleanup] = first

    async def work(_):
        native.event("work")
        return 7

    with pytest.raises(first_class) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first
    assert not native.reversible_closes
    if cleanup in ("stop", "flush"):
        assert native.events[-2:] == [cleanup, "ch"]
        assert not native.terminal_ends and "pg" not in native.events
        assert "certificate" not in native.events
    elif cleanup == "pg":
        assert native.events[-3:] == ["flush", "pg", "ch"]
        assert len(native.terminal_ends) == 1 and "certificate" not in native.events
    else:
        assert native.events[-3:] == ["pg", "certificate", "ch"]
        assert len(native.terminal_ends) == 1
    assert not native.client.connected


def test_first_required_cleanup_error_wins_and_all_safe_cleanup_runs(native, monkeypatch):
    first = OSError("keeper")

    async def keeper(_):
        raise first

    async def work(_):
        await asyncio.sleep(0)
        return 7

    monkeypatch.setattr(command, "_keep_overrides", keeper)
    native.errors.update(certificate=ValueError("retirement"), pg=RuntimeError("pg"))
    with pytest.raises(OSError) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first
    assert "stop" in native.events and "flush" in native.events
    assert native.events[-3:] == ["flush", "pg", "ch"]
    assert "certificate" not in native.events
    assert len(native.terminal_ends) == 1 and not native.reversible_closes


@pytest.mark.parametrize("first_class", FIRST)
def test_driver_restore_failure_remains_first_when_keeper_and_native_close_fail(
    native, monkeypatch, first_class
):
    first = first_class("driver work")

    async def keeper(_):
        raise OSError("keeper")

    async def work(_):
        await asyncio.sleep(0)
        try:
            raise first
        finally:
            native.events.append("restore")

    monkeypatch.setattr(command, "_keep_overrides", keeper)
    native.errors.update(certificate=OSError("certificate"), pg=OSError("pg"), ch=OSError("ch"))
    with pytest.raises(first_class) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first
    assert native.events.index("restore") < native.events.index("stop")
    assert native.events[-2:] == ["pg", "ch"]


@pytest.mark.parametrize("phase", ["drain", "flush", "retire"])
@pytest.mark.parametrize("first_class", [None, *FIRST])
def test_unacknowledged_cleanup_refuses_without_early_certification(
    native, monkeypatch, phase, first_class
):
    calls = []
    first = first_class("work") if first_class else None

    async def unacknowledged(*_, **__):
        calls.append(phase)
        return False

    target = {
        "drain": "drain_task_sets",
        "flush": "flush_registered_buffers",
        "retire": "_retire_until",
    }
    monkeypatch.setattr(command, target[phase], unacknowledged)

    async def work(_):
        if first is not None:
            raise first
        return 7

    with pytest.raises(first_class or RuntimeError) as caught:
        _ch.run_connected(work, orgs=("o",))
    if first is not None:
        assert caught.value is first
    assert calls == [phase]
    assert "certificate" not in native.events and "pg" not in native.events
    assert not native.terminal_ends and not native.reversible_closes
    assert native.events[-2:] == ["flush" if phase == "retire" else "stop", "ch"]


def test_actual_deferred_descendant_and_role_producer_end_before_certificate(native):
    async def work(_):
        async def producer():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                native.events.append("producer-ended")

        native.gate.track(asyncio.create_task(producer()))
        await asyncio.sleep(0)

        async def descendant():
            await asyncio.sleep(0)
            native.events.append("descendant")

        async def deferred():
            await asyncio.sleep(0)
            background.create_deferred_task(descendant())

        background.create_deferred_task(deferred())
        return 7

    assert _ch.run_connected(work, orgs=("o",)) == 7
    assert native.events.index("producer-ended") < native.events.index("certificate")
    assert native.events.index("descendant") < native.events.index("certificate")
    assert native.gate.quiescent and native.model_gate.active == 0
    assert native.events[-3:] == ["pg", "certificate", "ch"]


@pytest.mark.parametrize("first_class", [SystemExit, KeyboardInterrupt])
@pytest.mark.asyncio
async def test_override_keeper_returns_fatal_to_owner_before_loop_shutdown(first_class):
    first = first_class("keeper fatal")

    async def sleep(_):
        raise first

    assert await command._keep_overrides(True, sleep=sleep) is first


@pytest.mark.parametrize("first_class", [SystemExit, KeyboardInterrupt])
def test_real_override_keeper_fatal_is_retrieved_before_native_cleanup(
    native, monkeypatch, first_class
):
    first = first_class("keeper fatal")
    actual = command._keep_overrides

    async def sleep(_):
        raise first

    monkeypatch.setattr(command, "_keep_overrides", lambda loaded: actual(loaded, sleep=sleep))

    async def work(_):
        await asyncio.sleep(0)
        return 7

    with pytest.raises(first_class) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first
    assert native.events[-3:] == ["pg", "certificate", "ch"]
    assert native.gate.quiescent


def test_clean_native_success_has_one_certified_exit_and_no_keeper_left(native, monkeypatch):
    keepers = []
    actual = command._keep_overrides

    async def keeper(loaded):
        keepers.append(asyncio.current_task())
        return await actual(loaded)

    async def work(_):
        await asyncio.sleep(0)
        return 7

    monkeypatch.setattr(command, "_keep_overrides", keeper)
    assert _ch.run_connected(work, orgs=("o",)) == 7
    assert native.events.count("certificate") == native.events.count("pg") == 1
    assert len(native.terminal_ends) == 1 and not native.reversible_closes
    assert native.events[-3:] == ["pg", "certificate", "ch"]
    assert keepers and all(task.done() for task in keepers)


def test_caller_cancellation_during_keeper_stop_is_not_swallowed(native, monkeypatch):
    owner = []
    keepers = []

    async def keeper(_):
        keepers.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        finally:
            owner[0].cancel("cleanup cancellation")
            await asyncio.sleep(0)

    async def work(_):
        owner.append(asyncio.current_task())
        await asyncio.sleep(0)
        return 7

    monkeypatch.setattr(command, "_keep_overrides", keeper)
    with pytest.raises(asyncio.CancelledError, match="cleanup cancellation"):
        _ch.run_connected(work, orgs=("o",))
    assert native.events[-3:] == ["pg", "certificate", "ch"]
    assert keepers and all(task.done() for task in keepers)


def test_unfinished_role_producer_prevents_success_and_certificate(native, monkeypatch):
    producers = []
    monkeypatch.setattr(command.settings, "native_command_drain_timeout_s", 0)

    async def work(_):
        async def producer():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()

        task = native.gate.track(asyncio.create_task(producer()))
        producers.append(task)
        await asyncio.sleep(0)
        return 7

    with pytest.raises(RuntimeError, match="producers did not stop"):
        _ch.run_connected(work, orgs=("o",))
    assert "certificate" not in native.events and "pg" not in native.events
    assert not native.terminal_ends and not native.reversible_closes
    assert not native.gate.producers_stopped
    assert producers and all(task.done() for task in producers)


def test_first_role_producer_failure_is_retrieved_and_does_not_skip_safe_drain(native, monkeypatch):
    first = OSError("producer failed")
    producers = []

    async def work(_):
        async def producer(error):
            raise error

        # Completion callbacks run before native shutdown snapshots tracked producers.
        for error in [first, first]:
            task = asyncio.create_task(producer(error))
            native.gate.track(task)
            producers.append(task)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return 7

    with pytest.raises(OSError) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first and command.unfinished_line(first) is None
    assert native.events[-3:] == ["pg", "certificate", "ch"]
    assert all(task.done() and not task._log_traceback for task in producers)


def test_actual_retirement_refusal_stays_registered_and_refuses_success(native, monkeypatch):
    monkeypatch.setattr(command.settings, "native_command_drain_timeout_s", 0.02)
    stopped = []
    monkeypatch.setattr(
        barrier,
        "_HEARTBEAT",
        SimpleNamespace(signal_stop=lambda: stopped.append(True), running=True),
    )

    async def work(_):
        return 7

    with pytest.raises(TimeoutError):
        _ch.run_connected(work, orgs=("o",))
    assert stopped and all(stopped)
    assert "certificate" not in native.events
    assert native.gate.enrolled and native.gate.quiescent
    assert native.events[-2:] == ["pg", "ch"]


@pytest.mark.parametrize("missing", ["host proof", "authority record"])
def test_a_process_that_cannot_certify_succeeds_at_once_and_stays_registered(
    native, monkeypatch, caplog, missing
):
    if missing == "host proof":
        monkeypatch.setattr(barrier, "participant_proof", lambda: "")
        reason = "this host gives no process identity (no readable /proc)"
    else:
        native.client.query_one = AsyncMock(return_value=None)
        reason = (
            "this ClickHouse has not recorded its storage authority yet "
            "(a boot with LOOM_BOOT_MIGRATE on records it)"
        )

    async def work(_):
        return 7

    with caplog.at_level("WARNING", logger=command.__name__):
        assert _ch.run_connected(work, orgs=("o",)) == 7
    assert "certificate" not in native.events
    assert native.gate.enrolled and native.gate.quiescent
    assert len(native.terminal_ends) == 1 and not native.reversible_closes
    assert native.events[-3:] == ["flush", "pg", "ch"]
    warned = [r.getMessage() for r in caplog.records if r.name == command.__name__]
    assert [message.partition(": ")[2] for message in warned] == [reason]


def test_a_process_that_cannot_certify_still_fails_while_a_write_may_land(native, monkeypatch):
    monkeypatch.setattr(command.settings, "native_command_drain_timeout_s", 0.02)
    monkeypatch.setattr(barrier, "participant_proof", lambda: "")
    monkeypatch.setattr(barrier, "_server_side_settled", AsyncMock(return_value=False))

    async def work(_):
        return 7

    with pytest.raises(TimeoutError) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert "certificate" not in native.events
    assert native.events[-2:] == ["pg", "ch"]
    line = command.unfinished_line(caught.value)
    assert line and "\n" not in line
    assert line.startswith(
        "the command returned, but its shutdown did not complete (TimeoutError): "
    )


def test_a_failed_command_is_not_reported_as_an_unfinished_shutdown(native):
    first = ValueError("work")

    async def work(_):
        raise first

    with pytest.raises(ValueError) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first and command.unfinished_line(first) is None
