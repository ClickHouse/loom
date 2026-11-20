"""Every ClickHouse statement of `loom sql-access`: the scope rows in
`loom.sql_access` and the SQL users of the surface (server.insights.domain.sql_surface)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from server.core.ch import sequential_reads, uncaptured
from server.core.ch_names import database, quote
from server.core.repo_base import _CHClient, _default_client
from server.insights.domain import analysis as boundary
from server.insights.domain import sql_surface as surface

UserKind = Literal["", "surface", "other"]


def target_database() -> str:
    return quote(database())


async def held(
    user: str, *, ch: _CHClient | None = None
) -> tuple[set[tuple[str, str]], datetime | None]:
    """The (org, namespace) pairs `user` reads now, and the newest version any
    of its rows carries (None for none), read past every replica's lag so a
    rescope right after a grant sees it."""
    ch = ch or _default_client()
    with sequential_reads():
        rows = await ch.query_all(
            "SELECT org, namespace, granted, updated_at FROM loom.sql_access FINAL "
            "WHERE user_name = {u:String}",
            params={"u": user},
        )
    pairs = {(str(r["org"]), str(r["namespace"])) for r in rows if r["granted"]}
    return pairs, max((r["updated_at"] for r in rows), default=None)


async def grants(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Every granted scope row, by user."""
    ch = ch or _default_client()
    return await ch.query_all(
        "SELECT user_name, org, namespace, updated_at FROM loom.sql_access FINAL "
        "WHERE granted ORDER BY user_name, org, namespace"
    )


async def restricted(org: str, *, ch: _CHClient | None = None) -> set[tuple[str, str]]:
    """The namespaces of `org` (every org for ALL) the directory or the code
    registry classifies restricted; the views leave out the same."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT org, namespace FROM loom.namespaces FINAL "
        "WHERE classification = 'restricted' AND {org:String} IN ('*', org) "
        "UNION ALL SELECT org, namespace FROM loom.code_repos FINAL "
        "WHERE classification = 'restricted' AND {org:String} IN ('*', org)",
        params={"org": org},
    )
    return {(str(r["org"]), str(r["namespace"])) for r in rows}


async def write_rows(rows: list[dict[str, object]], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert("loom.sql_access", rows)


async def user_kind(user: str, *, ch: _CHClient | None = None) -> UserKind:
    """Whether a ClickHouse user named `user` exists, and if so whether it is
    a SQL user of the surface (it carries the surface's profile)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT count() AS n FROM system.users WHERE name = {u:String}", params={"u": user}
    )
    if not row or not int(row["n"]):
        return ""
    own = await ch.query_one(
        "SELECT count() AS n FROM system.settings_profile_elements "
        "WHERE user_name = {u:String} AND inherit_profile = {p:String}",
        params={"u": user, "p": surface.PROFILE},
    )
    return "surface" if own and int(own["n"]) else "other"


async def run(statements: list[str], *, ch: _CHClient | None = None) -> None:
    """Send access statements in order; the first refusal stops them. A
    statement may carry a password, so its text stays out of system.query_log
    and out of the statement capture file."""
    ch = ch or _default_client()
    with uncaptured():
        for sql in statements:
            await ch.execute(sql, settings=boundary.LOG_TEXT_CUT)
