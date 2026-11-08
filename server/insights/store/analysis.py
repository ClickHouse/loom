"""The analysis statements: writing the analyst user's access entities, the
boot probe, and running one `insights.query` statement as that user.

What the entities are is `server.insights.domain.analysis`; this module only
sends them.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from server.core.ch import as_analyst, own_write_reads, uncaptured
from server.core.ch_errors import error_code
from server.core.ch_names import database, quote
from server.core.repo_base import _CHClient, _default_client
from server.core.sql_literal import sql_string
from server.insights.domain import analysis as boundary

log = logging.getLogger(__name__)

# ClickHouse's answer to a read of a setting nobody set: an unsigned read of a
# table under Loom's scope policy, or a profile with no version stamp yet.
UNKNOWN_SETTING = 115


class _AnalystClient(_CHClient, Protocol):
    """The client the analyst's own reads need: rows with their column names
    and types, which only these reads ask for."""

    async def query_columns(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[str], list[tuple[Any, ...]]]: ...


def _analyst_client() -> _AnalystClient:
    from server.core.ch import get_client

    return get_client()


async def stored_entities(*, ch: _CHClient | None = None) -> boundary.Stored | None:
    """What the analyst's settings profile says it holds, or None when there
    is no such profile (or a write stopped before stamping it)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT setting_name, value
        FROM system.settings_profile_elements
        WHERE profile_name = {profile:String} AND setting_name IN {names:Array(String)}
        """,
        {
            "profile": boundary.ANALYST_USER,
            "names": [boundary.VERSION_SETTING, boundary.DIGEST_SETTING],
        },
    )
    found = {str(r["setting_name"]): str(r["value"]).strip("'") for r in rows}
    if boundary.DIGEST_SETTING not in found:
        return None
    try:
        version = int(found.get(boundary.VERSION_SETTING, "0"))
    except ValueError:
        # A stamp edited by hand reads as the oldest version, which a write replaces.
        version = 0
    return boundary.Stored(version, found[boundary.DIGEST_SETTING])


async def sealed_secrets(*, ch: _CHClient | None = None) -> str | None:
    """The sealed scope key and analyst password, or None before they were drawn.
    Read with read-your-writes, so a row another replica just wrote is seen."""
    ch = ch or _default_client()
    with own_write_reads():
        row = await ch.query_one("SELECT sealed FROM loom.analysis_secrets FINAL WHERE id = 0")
    return str(row["sealed"]) if row else None


async def store_secrets(sealed: str, *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(
        "loom.analysis_secrets",
        [{"id": 0, "sealed": sealed, "version": 1}],
        settings={"async_insert": 0},
    )


async def granted(*, ch: _CHClient | None = None) -> frozenset[boundary.Grant]:
    """Every grant the analyst holds now, partial revokes left out."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT access_type, ifNull(database, '') AS database, ifNull(table, '') AS table,
               ifNull(column, '') AS column, database = currentDatabase() AS home
        FROM system.grants
        WHERE user_name = {user:String} AND NOT is_partial_revoke
        """,
        {"user": boundary.ANALYST_USER},
    )
    return frozenset(
        boundary.Grant(
            str(r["access_type"]),
            str(r["database"]),
            str(r["table"]),
            str(r["column"]),
            bool(r["home"]),
        )
        for r in rows
    )


async def granted_roles(*, ch: _CHClient | None = None) -> frozenset[str]:
    """The roles granted to the analyst, which the allowlist never includes."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT granted_role_name FROM system.role_grants WHERE user_name = {user:String}",
        {"user": boundary.ANALYST_USER},
    )
    return frozenset(str(r["granted_role_name"]) for r in rows)


async def stored_caps_digest(*, ch: _CHClient | None = None) -> str:
    """The digest stamped on the caps profile ('' when there is none)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT value FROM system.settings_profile_elements
        WHERE profile_name = {profile:String} AND setting_name = {name:String}
        """,
        {"profile": boundary.CAPS_PROFILE, "name": boundary.CAPS_DIGEST_SETTING},
    )
    return str(row["value"]).strip("'") if row else ""


def foreign_policies_sql(db: str) -> str:
    """Permissive row policies on the allowlisted tables that Loom did not
    write, in the database the SQL expression `db` names. ClickHouse ORs
    permissive policies, so Loom's `USING 1` policy for every other user
    would widen what each of them lets its users read."""
    tables = ", ".join(sql_string(t) for t in boundary.allowlisted_tables())
    ours = ", ".join(sql_string(n) for n in (boundary.POLICY_NAME, boundary.OTHERS_POLICY_NAME))
    return (
        f"SELECT short_name, table FROM system.row_policies WHERE database = {db} "
        f"AND table IN ({tables}) AND NOT is_restrictive AND short_name NOT IN ({ours}) "
        "ORDER BY table, short_name"
    )


def other_deployments_sql(db: str) -> str:
    """Databases other than the one `db` names whose tables carry the
    analyst's scope policy: another Loom writing the same server-wide user."""
    return (
        "SELECT DISTINCT database FROM system.row_policies WHERE short_name = "
        f"{sql_string(boundary.POLICY_NAME)} AND database != {db} ORDER BY database"
    )


def printed_checks() -> list[str]:
    """Both checks naming Loom's database, for an admin whose session may
    sit in another one."""
    db = sql_string(database())
    return [f"{foreign_policies_sql(db)};", f"{other_deployments_sql(db)};"]


async def foreign_policies(*, ch: _CHClient | None = None) -> list[str]:
    ch = ch or _default_client()
    rows = await ch.query_all(foreign_policies_sql("currentDatabase()"))
    return [f"{r['short_name']} ON {r['table']}" for r in rows]


async def other_deployments(*, ch: _CHClient | None = None) -> list[str]:
    ch = ch or _default_client()
    rows = await ch.query_all(other_deployments_sql("currentDatabase()"))
    return [str(r["database"]) for r in rows]


async def secrets_table_exists(*, ch: _CHClient | None = None) -> bool:
    """Whether the analysis migration has run: `loom ch-setup` can run before
    Loom's first boot has migrated."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT count() AS n FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'analysis_secrets'"
    )
    return row is not None and int(row["n"]) > 0


async def analyst_user_absent(*, ch: _CHClient | None = None) -> bool:
    """True only when Loom's user can see that no analyst user exists; False
    when it exists or Loom's user cannot list users."""
    ch = ch or _default_client()
    try:
        row = await ch.query_one(
            "SELECT count() AS n FROM system.users WHERE name = {user:String}",
            {"user": boundary.ANALYST_USER},
        )
    except Exception:  # noqa: BLE001 — a user without access management cannot tell
        return False
    return row is not None and int(row["n"]) == 0


async def can_manage_access(*, ch: _CHClient | None = None) -> bool:
    """Whether Loom's own user can see the access entities, and so write them."""
    ch = ch or _default_client()
    try:
        await ch.query_one("SELECT count() AS n FROM system.users")
    except Exception:  # noqa: BLE001 — a user without access management cannot
        return False
    return True


async def own_version(
    *, password: str, extra: dict[str, Any], ch: _AnalystClient | None = None
) -> int:
    """The version stamped on the analyst's profile, read as the analyst (0 for none)."""
    ch = ch or _analyst_client()
    with as_analyst(boundary.ANALYST_USER, password, extra):
        try:
            _, _, rows = await ch.query_columns(
                f"SELECT toString(getSetting('{boundary.VERSION_SETTING}'))"
            )
        except Exception as e:
            if error_code(e) == UNKNOWN_SETTING:
                return 0  # no stamp yet: the setting is unknown
            raise
    return int(str(rows[0][0]).strip("'"))


_GRANT = re.compile(
    r"^GRANT SELECT\((?P<columns>[^)]*)\) ON (?P<database>[^.\s]+)\.(?P<table>\S+) TO "
)


async def own_grants(
    *, password: str, extra: dict[str, Any], ch: _AnalystClient | None = None
) -> frozenset[tuple[str, str]]:
    """The (table, column) pairs the analyst's own SHOW GRANTS names; any grant
    of another shape (a whole table, another privilege) comes back as
    ("*", <the grant>) so it can never match the allowlist."""
    ch = ch or _analyst_client()
    with as_analyst(boundary.ANALYST_USER, password, extra):
        _, _, rows = await ch.query_columns("SHOW GRANTS")
    out: set[tuple[str, str]] = set()
    for (line,) in rows:
        m = _GRANT.match(str(line))
        if m is None or m["database"].strip("`") != database():
            out.add(("*", str(line)))
            continue
        out |= {(m["table"], c.strip()) for c in m["columns"].split(",")}
    return frozenset(out)


async def own_grant_lines(
    *, password: str, extra: dict[str, Any], ch: _AnalystClient | None = None
) -> list[str]:
    """The analyst's own SHOW GRANTS, line by line, read as the analyst."""
    ch = ch or _analyst_client()
    with as_analyst(boundary.ANALYST_USER, password, extra):
        _, _, rows = await ch.query_columns("SHOW GRANTS")
    return [str(line) for (line,) in rows]


async def write_entities(statements: list[str], *, ch: _CHClient | None = None) -> None:
    """Send the entity statements in order; the first refusal stops them.
    They hold the analyst's password and the scope key, so they stay out of
    system.query_log and the statement capture file."""
    ch = ch or _default_client()
    with uncaptured():
        for sql in statements:
            await ch.execute(sql, settings={"log_queries": 0})


def target_database() -> str:
    return quote(database())


# Reads the analyst must be refused, each with what it reads: ClickHouse lets
# every user read system.one and the rows of system.tables and system.columns
# it holds grants on, so the probe asks for what it must not. LIMIT 1: a
# LIMIT 0 statement can finish before ClickHouse checks columns.
REFUSED_READS = {
    "system.query_log": "SELECT count() FROM system.query_log",
    "system.users": "SELECT count() FROM system.users",
    "memory.value": "SELECT toString(value) FROM {db}.memory LIMIT 1",
}


async def count_rows(table: str, *, password: str, extra: dict[str, Any]) -> int:
    """`count()` of one of Loom's tables as the analyst, under `extra`."""
    _, _, rows = await run_statement(
        f"SELECT count() FROM {target_database()}.{table}", password=password, extra=extra
    )
    return int(rows[0][0])


async def read_refused(name: str, *, password: str, extra: dict[str, Any]) -> None:
    """Run the read `REFUSED_READS` names as the analyst; ClickHouse's answer
    is raised."""
    sql = REFUSED_READS[name].format(db=target_database())
    await run_statement(sql, password=password, extra=extra)


async def read_unsigned(table: str, *, password: str) -> None:
    """Read one row of a table as the analyst with no scope settings; a table
    under Loom's scope policy raises UNKNOWN_SETTING."""
    await run_statement(
        f"SELECT 1 FROM {target_database()}.{table} LIMIT 1", password=password, extra={}
    )


async def run_statement(
    sql: str, *, password: str, extra: dict[str, Any], ch: _AnalystClient | None = None
) -> tuple[list[str], list[str], list[tuple[Any, ...]]]:
    """Column names, column types and rows of one statement run as the analyst
    user with `extra` (the signed scope and the caps) as its settings."""
    ch = ch or _analyst_client()
    with as_analyst(boundary.ANALYST_USER, password, extra):
        return await ch.query_columns(sql)
