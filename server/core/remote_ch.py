"""Read-only ClickHouse connections pinned to a vetted address."""

from typing import Any

from server.settings import settings


async def connect(
    host: str,
    *,
    address: str,
    port: int,
    secure: bool,
    database: str,
    user: str,
    password: str,
    client_name: str,
    send_receive_timeout: int,
    query_settings: dict[str, Any] | None = None,
) -> Any:
    """The one clickhouse-connect client builder for a tracker host. With a
    vetted `address` the client connects to it and keeps `host` as the Host
    header, TLS SNI and the certificate name, so a DNS answer that changes
    after the vet cannot point it elsewhere; with none it connects by name."""
    import clickhouse_connect

    return await clickhouse_connect.get_async_client(
        host=address or host,
        server_host_name=host if address else None,
        port=int(port),
        username=user,
        password=password,
        database=database,
        secure=bool(secure),
        connect_timeout=settings.tracker_source_connect_timeout_s,
        send_receive_timeout=send_receive_timeout,
        compress=True,
        client_name=client_name,
        # readonly=2: SELECT only, no DDL and no writes, but settings may be
        # set — readonly=1 also refuses the `remoteSecure` table function the
        # upstream's `version_history` / `checks` views are built on ("Cannot
        # execute query in readonly mode")
        settings={"readonly": 2, **(query_settings or {})},
    )
