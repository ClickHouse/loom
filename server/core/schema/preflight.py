"""What Loom checks on a ClickHouse before it serves or migrates it: the server
answers these credentials, is new enough, and holds the `loom` database with
the grants this process needs. Boot, `loom migrate`, `loom doctor` and
`loom ch-setup` run the same checks, and every failure says how to fix it in
one line."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from server.core.ch_errors import error_code
from server.core.paths import MIGRATIONS_DIR

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
# The system tables the migration runner reads (and the preflight's own): a
# refused one would fail a migration halfway, so it stops a migrating boot.
RUNNER_SYSTEM_TABLES = (
    "columns",
    "databases",
    "merge_tree_settings",
    "mutations",
    "one",
    "replicas",
    "settings",
    "tables",
)
# The ones Loom's telemetry and health views read; those fail soft, so a
# refused one is a warning. clusterAllReplicas also needs REMOTE.
TELEMETRY_SYSTEM_TABLES = (
    "asynchronous_inserts",
    "data_skipping_indices",
    "events",
    "merges",
    "part_log",
    "parts",
    "processes",
    "query_log",
    "view_refreshes",
    "zookeeper_connection",
)
SYSTEM_TABLES = RUNNER_SYSTEM_TABLES + TELEMETRY_SYSTEM_TABLES
# Every replica's version; a server without the `default` cluster is one node.
# Every configured replica must answer: one that does not is unverified, not
# absent. Set here, so a profile that skips unavailable shards cannot.
REPLICA_VERSIONS_SQL = (
    "SELECT version() FROM clusterAllReplicas('default', system.one) "
    "SETTINGS skip_unavailable_shards = 0"
)

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
    # Telemetry tables this user may not read: their views stay empty.
    unreadable: tuple[str, ...] = ()
    # What the next migration will refuse, passed now only because nothing
    # is pending.
    next_migration: tuple[PreflightError, ...] = ()


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
        # Every column: a column-level grant would pass `SELECT 1` yet fail the
        # runner's own queries.
        ch.command(f"SELECT * FROM {table} LIMIT 0")
    except Exception as e:
        if error_code(e) == ACCESS_DENIED:
            return False
        if error_code(e) in (60, 81):  # UNKNOWN_TABLE, UNKNOWN_DATABASE
            return True
        raise
    return True


def _migrations_pending(ch: Client, journal: Path) -> bool:
    """Whether the ledger lacks any migration in `journal`, the directory the
    command will apply (all of them where there is no ledger yet). A ledger
    that cannot be read, or no migration files to compare it with (an installed
    package that ships none), counts as pending: only a confirmed empty set
    relaxes the checks."""
    from server.core.schema.journal import sorted_migrations
    from server.core.schema.migrate import applied_migrations, ledger_report

    files = sorted_migrations(journal.glob("*.sql"))
    if not files:
        return True
    try:
        applied = applied_migrations(ch)
    except Exception:  # noqa: BLE001 — unknown means pending, the strict side
        return True
    return bool(ledger_report(applied, files).pending)


def oldest_version(ch: Client, *, strict: bool = True) -> str:
    """The oldest version among the server's replicas: a Cloud service in a
    rolling upgrade answers from replicas on two versions, and the old one
    still runs the DDL. A server without the `default` cluster is one node.
    When the replicas cannot be asked, `strict` (a process about to migrate)
    stops; otherwise the connected server answers (check() records what a
    migrating process would have refused)."""
    from server.settings import settings

    try:
        versions = [str(row[0]) for row in ch.query(REPLICA_VERSIONS_SQL).result_rows]
    except Exception as e:  # noqa: BLE001 — a missing cluster is one node; the rest depends on strict
        code = error_code(e)
        if code != CLUSTER_DOESNT_EXIST:
            if strict and code == ACCESS_DENIED:
                raise PreflightError(
                    f"{settings.ch_user!r} may not ask every replica for its version "
                    "(clusterAllReplicas): an admin has to run Loom's setup SQL",
                    needs_admin=True,
                ) from None
            if strict:
                reason = str(e).splitlines()[0].split(" (for url")[0][:200]
                raise PreflightError(f"cannot read every replica's version: {reason}") from None
        versions = [str(ch.command("SELECT version()"))]
    return min(versions, key=version_key)


def _granted(ch: Client, grants: str) -> bool:
    return str(ch.command(f"CHECK GRANT {grants} ON {DATABASE}.*")) == "1"


def _lacks(user: str, grants: str) -> PreflightError:
    return PreflightError(
        f"{user!r} lacks {grants} on {DATABASE}.*: "
        f"an admin has to run GRANT CURRENT GRANTS ON {DATABASE}.* TO `{user}`",
        needs_admin=True,
    )


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


def check(
    ch: Client,
    *,
    need: Need,
    create_database: bool = True,
    journal: Path = MIGRATIONS_DIR,
    strict: bool = False,
) -> Server:
    """Raise PreflightError unless the server is new enough and `loom` exists
    with the grants `need` names. A process that will migrate creates a missing
    database where its user may; with `create_database` off (the doctor) it
    only reports it missing. `strict` checks as if a migration were pending
    (ch-setup prepares for the next one). Returns what the doctor reports besides."""
    from server.settings import settings

    user = settings.ch_user
    database = bool(ch.command(_DATABASE_COUNT))
    # Strict where DDL is about to run: a restart with nothing pending is
    # checked like a process that only serves.
    migrating = need == "migrate" and (strict or not database or _migrations_pending(ch, journal))
    later: list[PreflightError] = []
    try:
        version = oldest_version(ch, strict=need == "migrate")
    except PreflightError as e:
        if migrating:
            raise
        later.append(e)
        version = str(ch.command("SELECT version()"))
    if version_key(version)[:2] < MIN_VERSION:
        floor = ".".join(map(str, MIN_VERSION))
        raise PreflightError(f"ClickHouse {version} is older than {floor}: upgrade the server")

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

    # The DDL grants are needed only while a migration is pending, like the
    # checks below; an up-to-date process needs those it serves with.
    deferred = need == "migrate" and not migrating
    if not _granted(ch, GRANTS["serve" if deferred else need]):
        raise _lacks(user, GRANTS["serve" if deferred else need])
    if deferred and not _granted(ch, GRANTS["migrate"]):
        later.append(_lacks(user, GRANTS["migrate"]))

    if need == "migrate":
        refused = [t for t in RUNNER_SYSTEM_TABLES if not _readable(ch, f"system.{t}")]
        if refused:
            error = PreflightError(
                f"{user!r} may not read system.{', system.'.join(refused)}, which the "
                "migrations need: an admin has to run Loom's setup SQL",
                needs_admin=True,
            )
            if migrating:
                raise error
            later.append(error)
    unreadable = tuple(t for t in TELEMETRY_SYSTEM_TABLES if not _readable(ch, f"system.{t}"))

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
        unreadable=unreadable,
        next_migration=tuple(later),
    )


def warnings(server: Server) -> str:
    """What a passing check still warns of, as one line ('' for nothing): the
    refusals the next migration will hit and the telemetry tables this user
    may not read. Boot and `loom migrate` log it; the doctor shows it."""
    parts = []
    if server.next_migration:
        reasons = "; ".join(gap.reason for gap in server.next_migration)
        parts.append(f"the next migration will refuse: {reasons}")
    if server.unreadable:
        tables = ", ".join(f"system.{t}" for t in server.unreadable)
        parts.append(f"may not read {tables}, so the telemetry from these tables stays empty")
    line = "; ".join(parts)
    if server.unreadable or any(gap.needs_admin for gap in server.next_migration):
        line += " (`loom ch-setup --print-sql` prints the SQL)"
    return line


def require_ready(
    *, need: Need, create_database: bool = True, journal: Path = MIGRATIONS_DIR
) -> Server:
    """`check` on a fresh connection, closed afterwards."""
    ch = connect()
    try:
        return check(ch, need=need, create_database=create_database, journal=journal)
    finally:
        ch.close()
