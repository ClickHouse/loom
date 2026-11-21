"""Tracker record work belongs to the deployment's entire native command lifetime."""

import asyncio
from types import SimpleNamespace

import pytest

from server.cli import _ch
from server.cli import tracker_feed as cli
from server.core import background
from server.core.model_work_gate import ModelChangeInProgress
from server.insights.app import provider_command as command
from tests.unit.test_cli_native_first_error import FIRST
from tests.unit.test_cli_native_first_error import native as native


@pytest.fixture
def tracker(native, monkeypatch):
    from server.core import ch, hosts, pg_profile
    from server.settings import settings
    from server.tracker.app import github_feed as feed
    from server.tracker.store import github_feed as store

    state = SimpleNamespace(native=native, strict=True, keepers=[])
    record = SimpleNamespace(connected=False)
    endpoint = SimpleNamespace(host="record.example", database="record", port=8443, secure=True)
    owner = "00000000-0000-0000-0000-000000000001"
    arguments = dict(
        org="team",
        host=endpoint.host,
        port=endpoint.port,
        insecure=False,
        database=endpoint.database,
        writer_user="writer",
        writer_password_env="TRACKER_OWNER_TEST_PASSWORD",
        source="registered",
        repo="team/project",
        token_env="TRACKER_OWNER_TEST_TOKEN",
        read_only_token=True,
        max_pages=5,
    )

    def owned():
        if state.strict:
            assert native.gate.role == "command" and native.gate.enrolled
            assert native.gate.active == native.model_gate.active == 1
            assert not native.gate.closed and native.client.connected

    async def verify(client):
        assert client is native.client
        if "verify" not in native.events:
            assert not native.gate.enrolled
        native.event("verify")

    async def keeper(loaded):
        state.keepers.append(asyncio.current_task())
        return await actual_keeper(loaded)

    async def deployment_owner(client):
        assert client is native.client
        owned()
        native.event("owner")
        return owner

    async def registered(args):
        assert args.org == "team" and args.source == "registered"
        owned()
        native.event("registered")
        return endpoint, {"team/project"}, "reader"

    async def vet(host, **kwargs):
        assert host == endpoint.host
        owned()
        native.event("vet")
        return "", "203.0.113.1"

    async def connect_record():
        owned()
        native.event("record-connect")
        record.connected = True

    async def close_record():
        owned()
        await asyncio.sleep(0)
        owned()
        record.connected = False
        native.event("record-close")

    async def validate(client, database):
        assert client is record and database == endpoint.database
        owned()
        native.event("validate")

    async def work():
        owned()
        native.event("work-enter")
        await asyncio.sleep(0)
        owned()
        native.event("work")

    async def initialize(client, org, owner_uuid):
        assert (client, org, owner_uuid) == (record, "team", owner)
        await work()

    async def configure(client, **kwargs):
        assert client is record
        assert kwargs == dict(
            org="team",
            owner_uuid=owner,
            repo="team/project",
            token="token",
            allowed_repos={"team/project"},
            max_pages=5,
        )
        await work()

    async def sync(client, **kwargs):
        assert client is record
        assert kwargs == dict(
            org="team", owner_uuid=owner, repo="team/project", allowed_repos={"team/project"}
        )
        await work()
        return {"synced": 1, "unchanged": 2}

    actual_keeper = command._keep_overrides
    monkeypatch.setattr(command, "_keep_overrides", keeper)
    monkeypatch.setattr(settings, "tracker_org", "team")
    monkeypatch.setattr(pg_profile, "check_configured", verify)
    monkeypatch.setattr(store, "database_uuid", deployment_owner)
    monkeypatch.setattr(cli, "_registered", registered)
    monkeypatch.setattr(hosts, "vet_host", vet)
    monkeypatch.setattr(ch, "ExternalCHClient", lambda **kwargs: record)
    monkeypatch.setattr(cli, "_validate_writer", validate)
    monkeypatch.setattr(feed, "initialize", initialize)
    monkeypatch.setattr(feed, "configure", configure)
    monkeypatch.setattr(feed, "sync", sync)
    monkeypatch.setenv("TRACKER_OWNER_TEST_PASSWORD", "password")
    monkeypatch.setenv("TRACKER_OWNER_TEST_TOKEN", "token")
    record.connect, record.disconnect = connect_record, close_record
    state.record, state.endpoint = record, endpoint
    state.args = lambda mode: SimpleNamespace(feed_command=mode, **arguments)
    return state


@pytest.fixture
def cleanup_tracker(tracker, monkeypatch):
    # Isolate cleanup defects from the separate missing-participant defect.
    tracker.strict = False

    async def acceptance(orgs):
        assert tuple(orgs) == ("team",)
        tracker.native.event("acceptance")

    monkeypatch.setattr(_ch, "check_orgs", acceptance)
    return tracker


def assert_closed(state):
    assert not state.record.connected and not state.native.client.connected
    assert state.native.gate.active == state.native.model_gate.active == 0
    assert not state.native.gate.producers
    assert not background._registered_tasks
    assert all(task.done() for task in state.keepers)


@pytest.mark.parametrize("mode", ["init", "configure", "sync"])
def test_tracker_run_owns_acceptance_all_work_and_record_close_until_certified(tracker, mode):
    result = asyncio.run(cli._run(tracker.args(mode)))
    assert (
        result
        == {
            "init": {"initialized": True},
            "configure": {"configured": True},
            "sync": {"synced": 1, "unchanged": 2},
        }[mode]
    )
    events = tracker.native.events
    assert events.index("verify") < events.index("enroll") < events.index("configuration")
    assert events.index("configuration") < events.index("acceptance") < events.index("owner")
    assert events.index("owner") < events.index("record-connect") < events.index("work-enter")
    assert events.index("work") < events.index("record-close") < events.index("stop")
    assert events[-3:] == ["certificate", "pg", "ch"]
    assert events.count("certificate") == 1 and tracker.native.gate.quiescent
    assert tracker.keepers
    assert_closed(tracker)


def test_tracker_run_refuses_closed_model_admission_before_record_or_work(tracker):
    tracker.native.model_gate.close("transition")
    with pytest.raises(ModelChangeInProgress):
        asyncio.run(cli._run(tracker.args("sync")))
    assert "record-connect" not in tracker.native.events
    assert "work-enter" not in tracker.native.events
    assert_closed(tracker)


@pytest.mark.parametrize("refusal", ["verify", "acceptance"])
def test_tracker_run_still_refuses_pairing_or_org_before_record_work(cleanup_tracker, refusal):
    state = cleanup_tracker
    first = ValueError("pairing") if refusal == "verify" else _ch.OrgNotHeld("org refused")
    state.native.errors[refusal] = first
    with pytest.raises(type(first)) as caught:
        asyncio.run(cli._run(state.args("init")))
    assert caught.value is first
    assert "owner" not in state.native.events and "record-connect" not in state.native.events
    assert "work-enter" not in state.native.events
    assert_closed(state)


@pytest.mark.parametrize("first_class", FIRST)
def test_tracker_run_preserves_work_failure_through_both_failed_closes(
    cleanup_tracker, first_class
):
    state = cleanup_tracker
    first = first_class("feed work")
    state.native.errors.update(
        work=first, **{"record-close": OSError("record close"), "pg": OSError("deployment close")}
    )
    with pytest.raises(first_class) as caught:
        asyncio.run(cli._run(state.args("sync")))
    assert caught.value is first
    events = state.native.events
    assert events.index("work") < events.index("record-close") < events.index("pg")
    assert events[-2:] == ["pg", "ch"] and events.count("record-close") == 1
    assert_closed(state)


def test_tracker_run_record_close_failure_stays_first_when_deployment_close_also_fails(
    cleanup_tracker,
):
    state = cleanup_tracker
    first = ValueError("record close")
    state.native.errors.update(**{"record-close": first, "pg": OSError("deployment close")})
    with pytest.raises(ValueError) as caught:
        asyncio.run(cli._run(state.args("sync")))
    assert caught.value is first
    assert state.native.events[-2:] == ["pg", "ch"]
    assert state.native.events.count("record-close") == 1
    assert_closed(state)


@pytest.mark.parametrize("first_class", [ValueError, asyncio.CancelledError])
def test_tracker_connection_preserves_validation_failure_when_record_close_fails(
    cleanup_tracker, first_class
):
    state = cleanup_tracker
    first = first_class("writer validation")
    state.native.errors.update(validate=first, **{"record-close": OSError("record close")})

    async def run():
        try:
            with pytest.raises(first_class) as caught:
                await cli._connection(state.args("init"), state.endpoint, state.native.client)
            assert caught.value is first
            assert state.native.events[-2:] == ["validate", "record-close"]
        finally:
            await _ch.disconnect(state.native.client)

    asyncio.run(run())
    assert_closed(state)


def test_tracker_connection_success_keeps_record_open_for_its_caller(cleanup_tracker):
    state = cleanup_tracker

    async def run():
        try:
            record = await cli._connection(state.args("init"), state.endpoint, state.native.client)
            assert record is state.record and record.connected
            assert state.native.events[-2:] == ["record-connect", "validate"]
            await record.disconnect()
        finally:
            await _ch.disconnect(state.native.client)

    asyncio.run(run())
    assert_closed(state)
