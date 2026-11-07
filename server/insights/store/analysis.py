"""The analysis statements: writing the analyst user's access entities, the
boot probe, and running one `insights.query` statement as that user.

What the entities are is `server.insights.domain.analysis`; this module only
sends them.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from server.core.ch import as_analyst, own_write_reads
from server.core.ch_names import database, quote
from server.core.repo_base import _CHClient, _default_client
from server.insights.domain import analysis as boundary

log = logging.getLogger(__name__)


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


async def own_version(*, password: str, extra: dict[str, Any], ch: _CHClient | None = None) -> int:
    """The version stamped on the analyst's profile, read as the analyst (0 for none)."""
    ch = ch or _default_client()
    with as_analyst(boundary.ANALYST_USER, password, extra):
        try:
            _, _, rows = await ch.query_columns(
                f"SELECT toString(getSetting('{boundary.VERSION_SETTING}'))"
            )
        except Exception:  # noqa: BLE001 — no stamp yet: the setting is unknown
            return 0
    try:
        return int(str(rows[0][0]).strip("'"))
    except (IndexError, ValueError):
        return 0


_GRANT = re.compile(r"^GRANT SELECT\((?P<columns>[^)]*)\) ON [^.]+\.(?P<table>\S+) TO ")


async def own_grants(
    *, password: str, extra: dict[str, Any], ch: _CHClient | None = None
) -> frozenset[tuple[str, str]]:
    """The (table, column) pairs the analyst's own SHOW GRANTS names; any grant
    of another shape (a whole table, another privilege) comes back as
    ("*", <the grant>) so it can never match the allowlist."""
    ch = ch or _default_client()
    with as_analyst(boundary.ANALYST_USER, password, extra):
        _, _, rows = await ch.query_columns("SHOW GRANTS")
    out: set[tuple[str, str]] = set()
    for (line,) in rows:
        m = _GRANT.match(str(line))
        if m is None:
            out.add(("*", str(line)))
            continue
        out |= {(m["table"], c.strip()) for c in m["columns"].split(",")}
    return frozenset(out)


async def write_entities(statements: list[str], *, ch: _CHClient | None = None) -> None:
    """Send the entity statements in order; the first refusal stops them.
    They hold the analyst's password hash and the scope key, so they stay
    out of system.query_log."""
    ch = ch or _default_client()
    for sql in statements:
        await ch.execute(sql, settings={"log_queries": 0})


def target_database() -> str:
    return quote(database())


async def run_statement(
    sql: str, *, password: str, extra: dict[str, Any], ch: _CHClient | None = None
) -> tuple[list[str], list[str], list[tuple[Any, ...]]]:
    """Column names, column types and rows of one statement run as the analyst
    user with `extra` (the signed scope and the caps) as its settings."""
    ch = ch or _default_client()
    with as_analyst(boundary.ANALYST_USER, password, extra):
        return await ch.query_columns(sql)
