"""The ClickHouse the `LOOM_CH_*` settings name, as clickhouse-connect client
arguments. The async client, the migration runner and the OTel exporter all
connect through it, so they agree on the target and on TLS."""

from __future__ import annotations

from typing import Any

from server.core.provider_process import client_name
from server.settings import Settings, settings


def ch_secure(s: Settings = settings) -> bool:
    """TLS: `LOOM_CH_SECURE` when set, else port 8443 (ClickHouse Cloud HTTPS)."""
    return bool(s.ch_secure) if s.ch_secure is not None else s.ch_port == 8443


def connection_kwargs(*, database: str | None = None) -> dict[str, Any]:
    """host, port, username, password, database and secure for
    `clickhouse_connect.get_client` / `get_async_client`; `database` overrides
    `LOOM_CH_DATABASE`. `client_name` tags every query with this process's
    provider-participant id (server/core/provider_process.py)."""
    return {
        "host": settings.ch_host,
        "port": settings.ch_port,
        "username": settings.ch_user,
        "password": settings.ch_password,
        "database": settings.ch_database if database is None else database,
        "secure": ch_secure(),
        "client_name": client_name(),
    }
