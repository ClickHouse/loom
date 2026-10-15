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

_DATABASE_COUNT = f"SELECT count() FROM system.databases WHERE name = '{DATABASE}'"

# UNKNOWN_USER, WRONG_PASSWORD, REQUIRED_PASSWORD, AUTHENTICATION_FAILED
AUTH_FAILED = frozenset({192, 193, 194, 516})
ACCESS_DENIED = 497
CLUSTER_DOESNT_EXIST = 701


class PreflightError(RuntimeError):
    """A check failed; the message names the fix. `needs_admin`: only a
    ClickHouse admin can apply it, and the text says where the SQL is; `reason`
    is the message without that pointer, for ch-setup, which prints the SQL."""

    def __init__(self, reason: str, *, needs_admin: bool = False) -> None:
        pointer = " (`loom ch-setup --print-sql` prints it)" if needs_admin else ""
        super().__init__(reason + pointer)
        self.reason = reason
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


def _readable(ch: Client, table: str) -> bool:
    """Whether this user may read `table`; one the server lacks counts as
    readable (part_log and view_refreshes appear on first use)."""
    try:
        ch.command(f"SELECT 1 FROM {table} LIMIT 0")
    except Exception as e:
        if error_code(e) == ACCESS_DENIED:
            return False
        if error_code(e) in (60, 81):  # UNKNOWN_TABLE, UNKNOWN_DATABASE
            return True
        raise
    return True


def oldest_version(ch: Client) -> str:
    """The oldest version among the server's replicas: a Cloud service in a
    rolling upgrade answers from replicas on two versions, and the old one
    still runs the DDL. Only a server without the `default` cluster is taken
    as one node; any other failure means the replicas are unknown."""
    try:
        versions = [str(row[0]) for row in ch.query(REPLICA_VERSIONS_SQL).result_rows]
    except Exception as e:  # noqa: BLE001 — every failure but a missing cluster stops here
        code = error_code(e)
        if code == ACCESS_DENIED:
            raise PreflightError(
                "may not ask every replica for its version (clusterAllReplicas): "
                "an admin has to run Loom's setup SQL",
                needs_admin=True,
            ) from None
        if code != CLUSTER_DOESNT_EXIST:
            raise PreflightError(
                f"cannot read every replica's version: {_connect_failure(e)}"
            ) from None
        versions = [str(ch.command("SELECT version()"))]
    return min(versions, key=version_key)


def _cannot_create(user: str) -> PreflightError:
    return PreflightError(
        f"{user!r} cannot see a database {DATABASE} and may not create it: "
        "an admin has to run Loom's setup SQL",
        needs_admin=True,
    )


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
    database = bool(ch.command(_DATABASE_COUNT))
    if not database:
        if need != "migrate":
            raise PreflightError(
                f"{user!r} cannot see a database {DATABASE} (missing, or no grant on it)",
                needs_admin=True,
            )
        if create_database:
            try:
                ch.command(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
            except Exception as e:
                if error_code(e) == ACCESS_DENIED:
                    raise _cannot_create(user) from None
                # Another process or replica may have created it meanwhile
                # (Cloud answers UNFINISHED, 341): only a still-missing one fails.
                if not ch.command(_DATABASE_COUNT):
                    raise
        # The doctor creates nothing; it only says whether boot could.
        elif str(ch.command(f"CHECK GRANT CREATE DATABASE ON {DATABASE}.*")) != "1":
            raise _cannot_create(user)

    grants = GRANTS[need]
    if str(ch.command(f"CHECK GRANT {grants} ON {DATABASE}.*")) != "1":
        raise PreflightError(
            f"{user!r} lacks {grants} on {DATABASE}.*: "
            f"an admin has to run GRANT CURRENT GRANTS ON {DATABASE}.* TO `{user}`",
            needs_admin=True,
        )

    # A refused table here would fail a migration halfway (the runner reads
    # system.merge_tree_settings, system.mutations and others as it goes).
    refused = [t for t in SYSTEM_TABLES if not _readable(ch, f"system.{t}")]
    if refused:
        raise PreflightError(
            f"{user!r} may not read system.{', system.'.join(refused)}: "
            "an admin has to run Loom's setup SQL",
            needs_admin=True,
        )

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
