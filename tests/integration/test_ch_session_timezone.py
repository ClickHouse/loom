"""Loom's ClickHouse sessions run in UTC whatever zone the server defaults to:
naive and aware datetime params bind as the same instant, and reads come back
naive UTC whatever the host's zone. A user whose settings profile sets another
`session_timezone` stands in for a server whose <timezone> is not UTC; one with
summer time is the case the driver would otherwise read in the host's zone."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from server.settings import settings

pytestmark = pytest.mark.usefixtures("ch_target_required")

_USER = "tz_probe"
_PASSWORD = "tz-probe-password"
_NOON = datetime(2026, 1, 1, 12, 0)
_SQL = (
    "SELECT timezone() AS tz, toUnixTimestamp({naive:DateTime}) AS naive, "
    "toUnixTimestamp({aware:DateTime}) AS aware, "
    "toDateTime('2026-01-01 12:00:00') AS read_back"
)


@pytest.fixture
def berlin_user(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from clickhouse_connect.driver import tzutil

    from server.core.schema.migrate import client

    monkeypatch.setattr(tzutil, "local_tz", ZoneInfo("America/New_York"))

    admin = client()
    admin.command(
        "CREATE SETTINGS PROFILE OR REPLACE tz_probe_profile "
        "SETTINGS session_timezone = 'Europe/Berlin'"
    )
    admin.command(
        f"CREATE USER OR REPLACE {_USER} IDENTIFIED BY '{_PASSWORD}' "
        "SETTINGS PROFILE 'tz_probe_profile'"
    )
    admin.command(f"GRANT SELECT ON *.* TO {_USER}")
    monkeypatch.setattr(settings, "ch_user", _USER)
    monkeypatch.setattr(settings, "ch_password", _PASSWORD)
    yield
    admin.command(f"DROP USER IF EXISTS {_USER}")
    admin.command("DROP SETTINGS PROFILE IF EXISTS tz_probe_profile")
    admin.close()


def _params() -> dict[str, Any]:
    return {"naive": _NOON, "aware": _NOON.replace(tzinfo=UTC)}


def _assert_utc(row: dict[str, Any]) -> None:
    noon = int(_NOON.replace(tzinfo=UTC).timestamp())
    assert row["tz"] == "UTC"
    assert row["naive"] == noon
    assert row["aware"] == noon
    assert row["read_back"] == _NOON


async def test_the_async_client_binds_and_reads_in_utc(berlin_user: None) -> None:
    from server.core.ch import CHClient

    c = CHClient()
    await c.connect()
    try:
        _assert_utc(await c.query_one(_SQL, _params()))
    finally:
        await c.disconnect()


def test_the_migration_client_binds_and_reads_in_utc(berlin_user: None) -> None:
    from server.core.schema.migrate import client

    ch = client()
    try:
        _assert_utc(next(ch.query(_SQL, parameters=_params()).named_results()))
    finally:
        ch.close()
