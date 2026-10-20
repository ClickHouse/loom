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

from server.core.ch_errors import error_code, error_line
from server.core.paths import MIGRATIONS_DIR

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client


DATABASE = "loom"
# Telemetry Loom derives from its own tables (045_request_sli.sql). Only the
# migrations write it: its refreshable view runs as the user that created it.
TELEMETRY_DATABASE = "loom_telemetry"
TELEMETRY_GRANTS = "CREATE DATABASE, CREATE TABLE, CREATE VIEW, INSERT, SELECT"
# The oldest server that applies every migration: 26.3 still gates stem() and
# 25.12 the text index. CI tests the latest release (dev/clickhouse_release.sh);
# features of newer servers (the Quantized codec, aiEmbed) are probed where used.
MIN_VERSION = (26, 4)
# The grants on loom.* each kind of process needs. A server, whether it
# migrates or not, sets retention TTLs (ALTER TABLE), compacts (OPTIMIZE) and
# rebuilds provider tables (CREATE, TRUNCATE, DROP) besides the migrations.
# ALTER TABLE and ALTER VIEW, not ALTER: from 26.9 the ALTER group holds ALTER
# HANDLER, which is global only, so `CHECK GRANT ALTER ON loom.*` fails even
# after GRANT ALL ON loom.*.
Need = Literal["server", "read"]
GRANTS: dict[Need, str] = {
    "server": "CREATE TABLE, CREATE VIEW, ALTER TABLE, ALTER VIEW, DROP TABLE, DROP VIEW, "
    "INSERT, SELECT, OPTIMIZE, TRUNCATE",
    "read": "SELECT",
}
# A server refreshes the code views with it after a purge, and tolerates a
# refusal (the scheduled refresh removes the rows anyway), so a missing one is
# a warning.
REFRESH_GRANT = "SYSTEM REFRESH VIEW"
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


def replica_versions_sql() -> str:
    """Every replica's version, over LOOM_CH_CLUSTER (unset: this server alone); a
    server without that cluster is one node. Every configured replica must answer:
    one that does not is unverified, not absent. Set here, so a profile that skips
    unavailable shards cannot."""
    from server.settings import settings

    if not settings.ch_cluster:
        return "SELECT version()"
    return (
        f"SELECT version() FROM clusterAllReplicas('{settings.ch_cluster}', system.one) "
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
    is the message without that pointer, for ch-setup, which prints the SQL.
    `no_keeper`: the server has no Keeper, which only its configuration can add."""

    def __init__(self, reason: str, *, needs_admin: bool = False, no_keeper: bool = False) -> None:
        pointer = " (`loom ch-setup --print-sql` prints it)" if needs_admin else ""
        super().__init__(reason + pointer)
        self.reason = reason
        self.needs_admin = needs_admin
        self.no_keeper = no_keeper


@dataclass(frozen=True)
class Server:
    version: str
    keeper: bool
    lightweight_updates: bool
    # False only when a check that may not create it found no `loom` database.
    database: bool = True
    # Telemetry tables this user may not read: their views stay empty.
    unreadable: tuple[str, ...] = ()
    # May not SYSTEM REFRESH VIEW: purged code lingers until the next refresh.
    no_refresh: bool = False
    # A server that does not migrate and may not read loom_telemetry.*: the
    # schema status and the refresh gauges cannot see the SLI rollup.
    no_telemetry_read: bool = False
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


def host_problem(host: str) -> str:
    """Why LOOM_CH_HOST cannot be a host name ('' when it can)."""
    if "://" in host or "/" in host:
        return (
            f"LOOM_CH_HOST={host!r} is not a host name: "
            "set the host alone, the port in LOOM_CH_PORT and TLS in LOOM_CH_SECURE"
        )
    return ""


def connect() -> Client:
    """A client on the `LOOM_CH_*` settings with no database selected, so it
    works before `loom` exists. Raises PreflightError for a wrong address or
    wrong credentials."""
    import clickhouse_connect

    from server.core.ch_target import ch_secure, connection_kwargs
    from server.settings import settings

    where = f"{settings.ch_host}:{settings.ch_port} ({'TLS' if ch_secure() else 'plain HTTP'})"
    if problem := host_problem(settings.ch_host):
        raise PreflightError(problem)
    # The driver logs its own traceback line for every failed connect; the
    # PreflightError below says it once.
    driver_log = logging.getLogger("clickhouse_connect")
    level = driver_log.level
    driver_log.setLevel(logging.CRITICAL)
    try:
        return clickhouse_connect.get_client(
            **connection_kwargs(database=""),
            connect_timeout=10,  # not a setting: the preflight's own probe, before any pool
            send_receive_timeout=60,  # not a setting: the preflight's own probe, before any pool
            settings={"log_comment": "ch.preflight"},
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


def oldest_version(
    ch: Client, *, strict: bool = True, gaps: list[PreflightError] | None = None
) -> str:
    """The oldest version among the server's replicas: a Cloud service in a
    rolling upgrade answers from replicas on two versions, and the old one
    still runs the DDL. A server without the LOOM_CH_CLUSTER cluster is one node.
    When the replicas cannot be asked, `strict` (a process about to migrate)
    stops; otherwise the connected server answers, and what a strict check
    would have refused goes to `gaps`."""
    from server.settings import settings

    try:
        versions = [str(row[0]) for row in ch.query(replica_versions_sql()).result_rows]
    except Exception as e:  # noqa: BLE001 — a missing cluster is one node; the rest depends on strict
        code = error_code(e)
        if code != CLUSTER_DOESNT_EXIST:
            if code == ACCESS_DENIED:
                refusal = PreflightError(
                    f"{settings.ch_user!r} may not run clusterAllReplicas("
                    f"'{settings.ch_cluster}', system.one) for every replica's version: an "
                    "admin has to run Loom's "
                    "setup SQL",
                    needs_admin=True,
                )
            else:
                reason = error_line(e)
                refusal = PreflightError(
                    f"cannot read every replica's version: {reason}; retry once every replica is up"
                )
            if strict:
                raise refusal from None
            if gaps is not None:
                gaps.append(refusal)
        versions = [str(ch.command("SELECT version()"))]
    return min(versions, key=version_key)


def _require(ch: Client, user: str, grants: str) -> None:
    """Raise, naming only the missing ones, unless `user` holds all of `grants`."""
    if missing := _missing(ch, grants):
        raise _lacks(user, missing)


def _missing(ch: Client, grants: str, database: str = DATABASE) -> str:
    """The ones of `grants` this user lacks on `database`.*, '' when it holds all."""
    if _granted(ch, grants, database):
        return ""
    return ", ".join(g for g in grants.split(", ") if not _granted(ch, g, database))


def _granted(ch: Client, grants: str, database: str = DATABASE) -> bool:
    return str(ch.command(f"CHECK GRANT {grants} ON {database}.*")) == "1"


def _lacks(user: str, grants: str, database: str = DATABASE) -> PreflightError:
    return PreflightError(
        f"{user!r} is missing {grants} on {database}.*: "
        f"an admin has to run GRANT CURRENT GRANTS ON {database}.* TO `{user}`",
        needs_admin=True,
    )


def _cannot_create(user: str) -> PreflightError:
    return PreflightError(
        f"{user!r} cannot see a database {DATABASE} and may not create it: "
        "an admin has to run Loom's setup SQL",
        needs_admin=True,
    )


def check(
    ch: Client,
    *,
    need: Need,
    create_database: bool = True,
    journal: Path = MIGRATIONS_DIR,
    strict: bool = False,
    migrates: bool = True,
) -> Server:
    """Raise PreflightError unless the server is new enough and `loom` exists
    with the grants `need` names. A process that `migrates` creates a missing
    database where its user may; with `create_database` off (the doctor) it
    only reports it missing. `strict` checks as if a migration were pending
    (ch-setup prepares for the next one). Returns what the doctor reports besides."""
    from server.settings import settings

    user = settings.ch_user
    database = bool(ch.command(_DATABASE_COUNT))
    migrates = migrates and need == "server"  # a replica or a read never migrates
    # Strict where migrations are about to run: a restart with nothing
    # pending, or one that does not migrate, relaxes what only they read.
    migrating = migrates and (strict or not database or _migrations_pending(ch, journal))
    later: list[PreflightError] = []
    version = oldest_version(ch, strict=migrating, gaps=later if migrates else None)
    if version_key(version)[:2] < MIN_VERSION:
        floor = ".".join(map(str, MIN_VERSION))
        raise PreflightError(f"ClickHouse {version} is older than {floor}: upgrade the server")

    if not database:
        if not migrates:
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

    # Strict even with nothing pending: the running server uses them too.
    _require(ch, user, GRANTS[need])
    no_refresh = need == "server" and not _granted(ch, REFRESH_GRANT)
    # A migrating process checks every telemetry grant below.
    no_telemetry_read = (
        need == "server" and not migrates and not _granted(ch, "SELECT", TELEMETRY_DATABASE)
    )

    if migrates:
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
        if missing := _missing(ch, TELEMETRY_GRANTS, TELEMETRY_DATABASE):
            error = _lacks(user, missing, TELEMETRY_DATABASE)
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
    if not keeper and need != "read":
        from server.core.schema.migrate_lease import KeeperRequired

        raise PreflightError(
            str(KeeperRequired("the server has no Keeper configured")), no_keeper=True
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
        no_refresh=no_refresh,
        no_telemetry_read=no_telemetry_read,
        next_migration=tuple(later),
    )


def warnings(server: Server) -> str:
    """What a passing check still warns of, as one line ('' for nothing): the
    refusals the next migration will hit and the telemetry tables this user
    may not read. Boot and `loom migrate` log it; the doctor shows it."""
    parts = []
    if server.next_migration:
        reasons = " | ".join(gap.reason for gap in server.next_migration)
        parts.append(f"the next migration will refuse: {reasons}")
    if server.unreadable:
        tables = ", ".join(f"system.{t}" for t in server.unreadable)
        parts.append(f"may not read {tables}, so the telemetry from these tables stays empty")
    if server.no_refresh:
        parts.append(
            f"may not {REFRESH_GRANT} on {DATABASE}.*, so purged code stays in the "
            "refreshable views until their next scheduled refresh"
        )
    if server.no_telemetry_read:
        parts.append(
            f"may not SELECT on {TELEMETRY_DATABASE}.*, so /v1/admin/schema.status reports the "
            "SLI rollup missing, the view refresh gauges leave it out, and "
            "loom.sli.rollup_lag_s and loom.sli.rollup_missed_rows are never reported"
        )
    # Each reason may end in its own SQL advice: " | " keeps them apart.
    line = " | ".join(parts)
    if (
        server.unreadable
        or server.no_refresh
        or server.no_telemetry_read
        or any(g.needs_admin for g in server.next_migration)
    ):
        line += " (`loom ch-setup --print-sql` prints the SQL)"
    return line


def require_ready(
    *,
    need: Need,
    create_database: bool = True,
    journal: Path = MIGRATIONS_DIR,
    migrates: bool = True,
) -> Server:
    """`check` on a fresh connection, closed afterwards."""
    ch = connect()
    try:
        return check(
            ch, need=need, create_database=create_database, journal=journal, migrates=migrates
        )
    finally:
        ch.close()
