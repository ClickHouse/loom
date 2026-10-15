"""What Loom checks on a ClickHouse before it serves or migrates it: the server
answers these credentials, is new enough, and holds the `loom` database with
the grants this process needs. Boot, `loom migrate`, `loom doctor` and
`loom ch-setup` run the same checks, and every failure says how to fix it in
one line."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from server.core.ch_errors import error_code

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client

DATABASE = "loom"
# The oldest server that applies every migration: 26.3 still gates stem() and
# 25.12 the text index. CI tests the latest release (dev/clickhouse_release.sh);
# features of newer servers (the Quantized codec, aiEmbed) are probed where used.
MIN_VERSION = (26, 4)
# The grants on loom.* each kind of process needs. ALTER TABLE and ALTER VIEW,
# not ALTER: from 26.9 the ALTER group holds ALTER HANDLER, which is global only,
# so `CHECK GRANT ALTER ON loom.*` fails even after GRANT ALL ON loom.*.
Need = Literal["migrate", "serve", "read"]
GRANTS: dict[Need, str] = {
    "migrate": "CREATE TABLE, CREATE VIEW, ALTER TABLE, ALTER VIEW, DROP TABLE, DROP VIEW, "
    "INSERT, SELECT",
    "serve": "SELECT, INSERT, ALTER UPDATE, ALTER DELETE",
    "read": "SELECT",
}
# The system tables Loom reads; clusterAllReplicas also needs REMOTE.
SYSTEM_TABLES = (
    "asynchronous_inserts",
    "columns",
    "data_skipping_indices",
    "databases",
    "events",
    "merge_tree_settings",
    "merges",
    "mutations",
    "one",
    "part_log",
    "parts",
    "processes",
    "query_log",
    "replicas",
    "settings",
    "tables",
    "view_refreshes",
    "zookeeper_connection",
)
# Every replica's version; a server without the `default` cluster is one node.
REPLICA_VERSIONS_SQL = "SELECT version() FROM clusterAllReplicas('default', system.one)"

# UNKNOWN_USER, WRONG_PASSWORD, REQUIRED_PASSWORD, AUTHENTICATION_FAILED
AUTH_FAILED = frozenset({192, 193, 194, 516})
ACCESS_DENIED = 497


class PreflightError(RuntimeError):
    """A check failed; the message names the fix. `needs_admin`: only a
    ClickHouse admin can apply it (`loom ch-setup --print-sql` has the SQL)."""

    def __init__(self, message: str, *, needs_admin: bool = False) -> None:
        super().__init__(message)
        self.needs_admin = needs_admin


@dataclass(frozen=True)
class Server:
    version: str
    keeper: bool
    lightweight_updates: bool
    # False only when a check that may not create it found no `loom` database.
    database: bool = True


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def _connect_failure(e: Exception) -> str:
    """The part of a driver connect error a person can act on."""
    text = str(e)
    if tls := re.search(r"\[SSL: (\w+)\]", text):
        return f"TLS handshake failed ({tls.group(1)}): is LOOM_CH_SECURE right for this port?"
    if errno := re.search(r"\[Errno -?\d+\] ([^\"')]+)", text):
        reason = errno.group(1)
    else:
        reason = text.splitlines()[0].split(" (for url")[0][:200]
    return f"{reason}; check LOOM_CH_HOST, LOOM_CH_PORT and LOOM_CH_SECURE"


def connect() -> Client:
    """A client on the `LOOM_CH_*` settings with no database selected, so it
    works before `loom` exists. Raises PreflightError for a wrong address or
    wrong credentials."""
    import clickhouse_connect

    from server.core.ch_target import ch_secure, connection_kwargs
    from server.settings import settings

    where = f"{settings.ch_host}:{settings.ch_port} ({'TLS' if ch_secure() else 'plain HTTP'})"
    if "://" in settings.ch_host or "/" in settings.ch_host:
        raise PreflightError(
            f"LOOM_CH_HOST={settings.ch_host!r} is not a host name: "
            "set the host alone, the port in LOOM_CH_PORT and TLS in LOOM_CH_SECURE"
        )
    # The driver logs its own traceback line for every failed connect; the
    # PreflightError below says it once.
    driver_log = logging.getLogger("clickhouse_connect")
    level = driver_log.level
    driver_log.setLevel(logging.CRITICAL)
    try:
        return clickhouse_connect.get_client(
            **connection_kwargs(database=""), connect_timeout=10, send_receive_timeout=60
        )
    except Exception as e:  # noqa: BLE001 — every connect failure becomes one line
        if error_code(e) in AUTH_FAILED:
            raise PreflightError(
                f"ClickHouse at {where} refused user {settings.ch_user!r}: "
                "check LOOM_CH_USER and LOOM_CH_PASSWORD"
            ) from None
        raise PreflightError(f"cannot reach ClickHouse at {where}: {_connect_failure(e)}") from None
    finally:
        driver_log.setLevel(level)


def oldest_version(ch: Client) -> str:
    """The oldest version among the server's replicas: a Cloud service in a
    rolling upgrade answers from replicas on two versions, and the old one
    still runs the DDL."""
    try:
        versions = [str(row[0]) for row in ch.query(REPLICA_VERSIONS_SQL).result_rows]
    except Exception:  # noqa: BLE001 — no `default` cluster, or no REMOTE grant: one server answers
        versions = []
    return min(versions or [str(ch.command("SELECT version()"))], key=version_key)


def needed_at_boot() -> Need:
    """What the server process needs: it migrates unless boot migrations are
    off or it is a read replica."""
    from server.settings import settings

    return "migrate" if settings.boot_migrate and not settings.read_replica else "serve"


def check(ch: Client, *, need: Need, create_database: bool = True) -> Server:
    """Raise PreflightError unless the server is new enough and `loom` exists
    with the grants `need` names. A process that will migrate creates a missing
    database where its user may; with `create_database` off (the doctor) it
    only reports it missing. Returns what the doctor reports besides."""
    from server.settings import settings

    version = oldest_version(ch)
    if version_key(version)[:2] < MIN_VERSION:
        floor = ".".join(map(str, MIN_VERSION))
        raise PreflightError(f"ClickHouse {version} is older than {floor}: upgrade the server")

    user = settings.ch_user
    database = bool(ch.command(f"SELECT count() FROM system.databases WHERE name = '{DATABASE}'"))
    if not database and need != "migrate":
        raise PreflightError(f"database {DATABASE} does not exist: run `loom ch-setup`")
    if not database and create_database:
        try:
            ch.command(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
        except Exception as e:
            if error_code(e) != ACCESS_DENIED:
                raise
            raise PreflightError(
                f"{user!r} cannot see a database {DATABASE} and may not create it: "
                f"run `loom ch-setup --print-sql` and have an admin apply it",
                needs_admin=True,
            ) from None

    grants = GRANTS[need]
    if str(ch.command(f"CHECK GRANT {grants} ON {DATABASE}.*")) != "1":
        raise PreflightError(
            f"{user!r} lacks {grants} on {DATABASE}.*: "
            f"have an admin run GRANT CURRENT GRANTS ON {DATABASE}.* TO `{user}`",
            needs_admin=True,
        )

    try:
        ch.command("SELECT count() FROM system.mutations WHERE 0")
    except Exception as e:
        if error_code(e) != ACCESS_DENIED:
            raise
        raise PreflightError(
            f"{user!r} may not read the system tables Loom waits on: "
            "run `loom ch-setup --print-sql` and have an admin apply its system grants",
            needs_admin=True,
        ) from None

    # The table exists only where the server is configured with a Keeper.
    keeper = bool(
        ch.command(
            "SELECT count() FROM system.tables "
            "WHERE database = 'system' AND name = 'zookeeper_connection'"
        )
    )
    lightweight = str(
        ch.command("SELECT value FROM system.settings WHERE name = 'enable_lightweight_update'")
    ) in ("1", "true")
    return Server(
        version=version,
        keeper=keeper,
        lightweight_updates=lightweight,
        database=database or create_database,
    )


def require_ready(*, need: Need, create_database: bool = True) -> Server:
    """`check` on a fresh connection, closed afterwards."""
    ch = connect()
    try:
        return check(ch, need=need, create_database=create_database)
    finally:
        ch.close()
