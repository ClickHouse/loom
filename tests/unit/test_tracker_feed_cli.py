"""Operator errors stay actionable without exposing database credentials."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.cli import tracker_feed as cli
from server.core.github_values import FeedInputError


def test_missing_credential_names_only_the_environment_variable(monkeypatch):
    monkeypatch.delenv("FEED_TEST_MISSING_SECRET", raising=False)
    with pytest.raises(FeedInputError, match="FEED_TEST_MISSING_SECRET"):
        cli._environment("FEED_TEST_MISSING_SECRET")


@pytest.mark.asyncio
async def test_deployment_connect_failure_still_disconnects(monkeypatch):
    from server.core import ch

    deployment = SimpleNamespace(
        connected=False, connect=AsyncMock(side_effect=OSError("failed")), disconnect=AsyncMock()
    )
    monkeypatch.setattr(ch, "get_client", lambda: deployment)
    from server.settings import settings

    monkeypatch.setattr(settings, "tracker_org", "team")
    with pytest.raises(OSError):
        await cli._run(SimpleNamespace(feed_command="init", org="team"))
    deployment.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_connect_failure_still_disconnects(monkeypatch):
    from server.cli import _ch
    from server.core import ch, hosts

    client = SimpleNamespace(
        connect=AsyncMock(side_effect=OSError("failed")), disconnect=AsyncMock()
    )
    monkeypatch.setattr(ch, "ExternalCHClient", lambda **kwargs: client)
    monkeypatch.setattr(hosts, "vet_host", AsyncMock(return_value=("", "203.0.113.1")))
    monkeypatch.setenv("FEED_TEST_WRITER_PASSWORD", "secret")
    endpoint = SimpleNamespace(host="record.example", database="record", port=8443, secure=True)
    args = SimpleNamespace(
        org="team", writer_user="writer", writer_password_env="FEED_TEST_WRITER_PASSWORD"
    )
    deployment = SimpleNamespace()
    monkeypatch.setattr(_ch, "connect_checked", AsyncMock())
    with pytest.raises(OSError):
        await cli._connection(args, endpoint, deployment)
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grant",
    [
        "GRANT ALL ON *.* TO writer",
        "GRANT INSERT ON loom.* TO writer",
        "GRANT broad_role TO writer",
    ],
)
async def test_record_connection_refuses_broad_or_role_writer_grants(monkeypatch, grant):
    from server.cli import _ch
    from server.core import ch, hosts

    client = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        query_all=AsyncMock(return_value=[{"grant": grant}]),
    )
    monkeypatch.setattr(ch, "ExternalCHClient", lambda **kwargs: client)
    monkeypatch.setattr(hosts, "vet_host", AsyncMock(return_value=("", "203.0.113.1")))
    monkeypatch.setenv("FEED_TEST_WRITER_PASSWORD", "secret")
    endpoint = SimpleNamespace(host="record.example", database="record", port=8443, secure=True)
    args = SimpleNamespace(
        org="team", writer_user="writer", writer_password_env="FEED_TEST_WRITER_PASSWORD"
    )
    deployment = SimpleNamespace()
    monkeypatch.setattr(_ch, "connect_checked", AsyncMock())
    with pytest.raises(FeedInputError, match="writer grants"):
        await cli._connection(args, endpoint, deployment)
    client.disconnect.assert_awaited_once()


def test_unexpected_failure_does_not_print_secret(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run", AsyncMock(side_effect=OSError("password=secret")))
    assert cli._command(SimpleNamespace()) == 1
    assert capsys.readouterr().err == "tracker feed refused: OSError\n"


@pytest.mark.asyncio
async def test_init_refuses_unusable_org_before_connecting(monkeypatch):
    from server.core import ch
    from server.settings import settings

    connect = AsyncMock()
    monkeypatch.setattr(settings, "tracker_org", "team")
    monkeypatch.setattr(ch, "get_client", connect)
    with pytest.raises(FeedInputError, match="configured tracker organization"):
        await cli._run(SimpleNamespace(feed_command="init", org="other"))
    connect.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine",
    [
        "",
        "ReplacingMergeTree",
        "SharedReplacingMergeTree",
        "ReplicatedReplacingMergeTree",
        "Distributed",
        "UnknownEngine",
    ],
)
async def test_writer_refuses_record_engines_without_acknowledged_read_consistency(engine):
    from server.tracker.store import github_feed as store

    async def query_all(sql, params=None):
        if sql.startswith("SHOW GRANTS"):
            return [
                {
                    "grant": "GRANT SELECT, INSERT, CREATE TABLE, CREATE VIEW ON team_record.* TO writer"
                }
            ]
        assert params == {"tables": list(store.TABLES)}
        assert "database = currentDatabase()" in sql
        return [{"name": "feed_repositories", "engine": engine}] if engine else []

    client = SimpleNamespace(query_all=query_all)
    if engine in {"", "ReplacingMergeTree", "SharedReplacingMergeTree"}:
        await cli._validate_writer(client, "team_record")
    else:
        with pytest.raises(FeedInputError, match="unsupported feed record table engine"):
            await cli._validate_writer(client, "team_record")


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["authority", "cell"])
async def test_init_refuses_deployment_admission_before_record_or_write(monkeypatch, refusal):
    from server.cli import _ch
    from server.core import ch, pg, pg_profile
    from server.settings import settings
    from server.tracker.app import github_feed as feed
    from server.tracker.store import github_feed as store

    deployment = SimpleNamespace(connected=False, connect=AsyncMock(), disconnect=AsyncMock())
    record = SimpleNamespace(disconnect=AsyncMock())
    admission_error = (
        OSError("wrong deployment") if refusal == "authority" else _ch.OrgNotHeld("not held")
    )
    authority = AsyncMock(side_effect=admission_error if refusal == "authority" else None)
    held = AsyncMock(side_effect=admission_error if refusal == "cell" else None)
    monkeypatch.setattr(settings, "tracker_org", "team")
    monkeypatch.setattr(ch, "get_client", lambda: deployment)
    monkeypatch.setattr(pg_profile, "check_configured", authority)
    monkeypatch.setattr(_ch, "check_orgs", held)
    monkeypatch.setattr(pg, "close_pool", AsyncMock())
    owner = AsyncMock(return_value="owner")
    monkeypatch.setattr(store, "database_uuid", owner)
    external = AsyncMock(return_value=record)
    monkeypatch.setattr(cli, "_connection", external)
    initialize = AsyncMock()
    monkeypatch.setattr(feed, "initialize", initialize)
    args = SimpleNamespace(
        feed_command="init",
        org="team",
        host="record.example",
        port=8443,
        insecure=False,
        database="record",
    )

    with pytest.raises(type(admission_error), match=str(admission_error)):
        await cli._run(args)

    authority.assert_awaited_once_with(deployment)
    if refusal == "cell":
        held.assert_awaited_once_with(("team",))
    else:
        held.assert_not_awaited()
    owner.assert_not_awaited()
    external.assert_not_awaited()
    initialize.assert_not_awaited()
    deployment.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_init_checks_actual_deployment_before_external_record_and_closes_both(monkeypatch):
    from server.cli import _ch
    from server.core import ch, hosts, pg, pg_profile
    from server.settings import settings
    from server.tracker.app import github_feed as feed
    from server.tracker.store import github_feed as store

    events = []
    deployment = SimpleNamespace(connected=False)

    async def deployment_connect():
        events.append("deployment connect")
        deployment.connected = True

    async def deployment_disconnect():
        events.append("deployment disconnect")

    async def authority(client):
        assert client is deployment
        events.append("authority")

    async def held(orgs):
        assert orgs == ("team",)
        events.append("held")

    async def record_connect():
        events.append("record connect")

    async def record_disconnect():
        events.append("record disconnect")

    async def owner(client):
        assert client is deployment
        events.append("owner")
        return "owner"

    async def vet(*args, **kwargs):
        events.append("vet")
        return "", "203.0.113.1"

    deployment.connect = AsyncMock(side_effect=deployment_connect)
    deployment.disconnect = AsyncMock(side_effect=deployment_disconnect)
    record = SimpleNamespace(
        connect=AsyncMock(side_effect=record_connect),
        disconnect=AsyncMock(side_effect=record_disconnect),
    )
    checked = AsyncMock(side_effect=authority)
    monkeypatch.setattr(settings, "tracker_org", "team")
    monkeypatch.setattr(ch, "get_client", lambda: deployment)
    monkeypatch.setattr(ch, "ExternalCHClient", lambda **kwargs: record)
    monkeypatch.setattr(pg_profile, "check_configured", checked)
    monkeypatch.setattr(_ch, "check_orgs", AsyncMock(side_effect=held))
    close_pool = AsyncMock()
    monkeypatch.setattr(pg, "close_pool", close_pool)
    monkeypatch.setattr(hosts, "vet_host", AsyncMock(side_effect=vet))
    monkeypatch.setattr(store, "database_uuid", AsyncMock(side_effect=owner))
    validated = AsyncMock()
    monkeypatch.setattr(cli, "_validate_writer", validated)
    initialize = AsyncMock()
    monkeypatch.setattr(feed, "initialize", initialize)
    monkeypatch.setenv("FEED_TEST_WRITER_PASSWORD", "secret")
    args = SimpleNamespace(
        feed_command="init",
        org="team",
        host="record.example",
        port=8443,
        insecure=False,
        database="record",
        writer_user="writer",
        writer_password_env="FEED_TEST_WRITER_PASSWORD",
    )

    assert await cli._run(args) == {"initialized": True}

    assert checked.await_count >= 1
    assert all(call.args == (deployment,) for call in checked.await_args_list)
    assert events.index("authority") < events.index("held") < events.index("owner")
    assert events.index("owner") < events.index("vet") < events.index("record connect")
    validated.assert_awaited_once_with(record, "record")
    initialize.assert_awaited_once_with(record, "team", "owner")
    record.connect.assert_awaited_once()
    record.disconnect.assert_awaited_once()
    deployment.disconnect.assert_awaited_once()
    close_pool.assert_awaited_once()
    assert events[-2:] == ["record disconnect", "deployment disconnect"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["authority", "cell"])
async def test_external_record_connect_refuses_unadmitted_deployment(monkeypatch, refusal):
    from server.cli import _ch
    from server.core import pg, pg_profile

    deployment = SimpleNamespace(connected=True, disconnect=AsyncMock())
    record = SimpleNamespace(connect=AsyncMock())
    error = OSError("wrong deployment") if refusal == "authority" else _ch.OrgNotHeld("not held")
    authority = AsyncMock(side_effect=error if refusal == "authority" else None)
    held = AsyncMock(side_effect=error if refusal == "cell" else None)
    close_pool = AsyncMock()
    monkeypatch.setattr(pg_profile, "check_configured", authority)
    monkeypatch.setattr(_ch, "check_orgs", held)
    monkeypatch.setattr(pg, "close_pool", close_pool)

    with pytest.raises(type(error), match=str(error)):
        await _ch.connect_external(record, deployment=deployment, orgs=("team",))

    authority.assert_awaited_once_with(deployment)
    if refusal == "cell":
        held.assert_awaited_once_with(("team",))
    else:
        held.assert_not_awaited()
    record.connect.assert_not_awaited()
    deployment.disconnect.assert_awaited_once()
    close_pool.assert_awaited_once()
