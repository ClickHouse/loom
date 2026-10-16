"""One builder names the ClickHouse target for the async client, the migration
runner and the OTel exporter, so all three agree on TLS: `LOOM_CH_SECURE`
when set, else port 8443."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.core import ch_target
from server.core.provider_process import participant_tag, process_identity
from server.settings import settings


@pytest.mark.parametrize(
    ("secure", "port", "tls"),
    [
        (None, 8123, False),
        (None, 8443, True),
        (True, 8123, True),
        (True, 8443, True),
        (False, 8123, False),
        (False, 8443, False),
    ],
)
def test_tls_follows_the_setting_then_the_port(
    monkeypatch: pytest.MonkeyPatch, secure: bool | None, port: int, tls: bool
) -> None:
    monkeypatch.setattr(settings, "ch_secure", secure)
    monkeypatch.setattr(settings, "ch_port", port)
    assert ch_target.ch_secure() is tls
    assert ch_target.connection_kwargs()["secure"] is tls


def test_kwargs_name_the_configured_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_host", "ch.example")
    monkeypatch.setattr(settings, "ch_port", 9440)
    monkeypatch.setattr(settings, "ch_user", "u")
    monkeypatch.setattr(settings, "ch_password", "p")
    monkeypatch.setattr(settings, "ch_database", "loomdb")
    monkeypatch.setattr(settings, "ch_secure", None)
    assert ch_target.connection_kwargs() == {
        "host": "ch.example",
        "port": 9440,
        "username": "u",
        "password": "p",
        "database": "loomdb",
        "secure": False,
        "client_name": participant_tag(process_identity()),
    }
    assert ch_target.connection_kwargs(database="default")["database"] == "default"


def test_the_migration_runner_and_the_otel_exporter_connect_through_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clickhouse_connect

    from server.core.schema import migrate
    from server.observability import otel_exporters

    seen: list[dict[str, Any]] = []

    def fake_get_client(**kw: Any) -> Any:
        seen.append(kw)
        return SimpleNamespace()

    monkeypatch.setattr(clickhouse_connect, "get_client", fake_get_client)
    monkeypatch.setattr(settings, "ch_secure", False)
    monkeypatch.setattr(settings, "ch_port", 8443)
    migrate.client()
    otel_exporters._make_ch_client_for_otel()
    # An explicit LOOM_CH_SECURE=false on 8443 is plain HTTP for both.
    assert [kw["secure"] for kw in seen] == [False, False]
    assert seen[0]["database"] == settings.ch_database and seen[1]["database"] == "default"
