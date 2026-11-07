"""Repository for loom.namespace_grants.

Per-namespace authz grants. ReplacingMergeTree(version) collapses
on (org, namespace, subject) so a new grant supersedes the older
one; revocation is a separate INSERT with `revoked_at` populated
+ a higher version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from server.core import grant_cache
from server.core.datetimes import to_naive_utc
from server.core.grant_sql import (
    GRANT_RANK_SQL,
    LIVE_GRANT,
    VERIFIED_EMAIL_SQL,
    grant_rank_sql,
    has_aliases_sql,
)
from server.core.principals import AGENT, EVERYONE, GROUP, SERVICE_ACCOUNT
from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.grants import GrantPerson, GrantState, PendingGrant


async def insert_row(
    row: dict[str, Any],
    *,
    announce: bool = True,
    ch: _CHClient | None = None,
) -> None:
    """Insert one grant row. Used by both create and revoke (revoke is
    a new row with `revoked_at` populated + bumped version). `announce=False`
    leaves the watermark to a caller that writes many rows and moves it once."""
    ch = ch or _default_client()
    try:
        await ch.insert(
            "loom.namespace_grants",
            [row],
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
    finally:
        if announce:
            await _changed([row])  # a reply lost after the write landed still drops it


async def insert_rows(
    rows: list[dict[str, Any]],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert several grant rows in ONE synchronous statement. A mint that
    grants a token eleven restricted namespaces paid eleven inserts at
    1.6 s p50 / 5 s p95 each on a Cloud cluster; one statement costs one."""
    if not rows:
        return
    ch = ch or _default_client()
    try:
        await ch.insert(
            "loom.namespace_grants",
            rows,
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
    finally:
        await _changed(rows)


async def _changed(rows: list[dict[str, Any]]) -> None:
    """This process's namespace gate sees a grant change at once; every other
    one at its next watermark poll (`grant_cache.changed`)."""
    for org in {r["org"] for r in rows}:
        await grant_cache.changed(org, [r["subject"] for r in rows if r["org"] == org])


async def pending_for_admins(
    org: str,
    subjects: list[str],
    *,
    limit: int = 500,
    ch: _CHClient | None = None,
) -> list[PendingGrant]:
    """Every pending grant (a join request, or an elevation request on an
    existing grant) in the namespaces where any of `subjects` — one person's
    spellings — holds an active admin grant, excluding the person's own
    requests. One statement for the approvals bell, instead of one
    `grants.list` per administered namespace every 30 s."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT p.namespace AS namespace,
               ifNull(n.kind, 'memory') AS kind,
               p.subject AS subject,
               p.role AS role,
               p.status AS status,
               p.pending_role AS pending_role,
               p.pending_since AS pending_since,
               p.granted_at AS granted_at
        FROM (
            SELECT namespace, subject, role,
                   ifNull(toString(status), 'active') AS status,
                   pending_role, pending_since, granted_at
            FROM loom.namespace_grants FINAL
            WHERE org = {org:String}
              AND revoked_at IS NULL
              AND (toString(status) = 'pending' OR pending_role IS NOT NULL)
              AND subject NOT IN {subs:Array(String)}
              AND namespace IN (
                  SELECT namespace FROM loom.namespace_grants FINAL
                  WHERE org = {org:String} AND subject IN {subs:Array(String)}
                    AND role = 'admin' AND {live}
              )
        ) AS p
        LEFT JOIN (
            SELECT namespace, kind FROM loom.namespaces FINAL WHERE org = {org:String}
        ) AS n ON n.namespace = p.namespace
        ORDER BY pending_since DESC, granted_at DESC
        LIMIT {lim:UInt32}
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "subs": subjects, "lim": int(limit)},
    )
    return [PendingGrant.from_row(r) for r in rows]


async def list_active(
    org: str,
    namespace: str,
    *,
    limit: int = 100,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Grants on (org, namespace) — revoked rows excluded. Includes BOTH
    active and pending grants; callers filter by `status` field if they
    only want one or the other."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT subject, role, status, pending_role, pending_since,
               granted_at, granted_by
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND revoked_at IS NULL
        ORDER BY granted_at DESC
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )


# Fold live spellings before the keyset and limit. Active rows set access;
# pending rows contribute requests without raising the active role.
_PEOPLE_PAGE = """
SELECT head.1 AS subject,
       if(active_rows > 0, active_role, head.7) AS role,
       if(active_rows > 0, 'active', head.2) AS status,
       if(active_rows > 0, request.1, head.3) AS pending_role,
       if(active_rows > 0, request.2, head.4) AS pending_since,
       head.5 AS granted_at, head.6 AS granted_by,
       arrayMap(k -> k.2, arrayFilter(k -> lowerUTF8(k.2) != lowerUTF8(head.1),
                                      arrayReverseSort(spellings))) AS also
FROM (
    SELECT argMax(tuple(subject, status, pending_role, pending_since, granted_at, granted_by, role),
                  tuple(granted_at, subject)) AS head,
           countIf({live}) AS active_rows,
           argMaxIf(role, {rank}, {live}) AS active_role,
           argMaxIf(tuple(asked, asked_at), tuple({rank_asked}, granted_at, subject),
                    asked IS NOT NULL) AS request,
           groupArray(tuple(granted_at, subject)) AS spellings
    FROM (
        SELECT g.subject AS subject, g.role AS role, g.status AS status,
               g.revoked_at AS revoked_at,
               g.pending_role AS pending_role, g.pending_since AS pending_since,
               g.granted_at AS granted_at, g.granted_by AS granted_by,
               -- What the row asks for: a pending row's role, or an active row's elevation.
               if(g.status = 'pending', g.role, g.pending_role) AS asked,
               if(g.status = 'pending', ifNull(g.pending_since, g.granted_at),
                  g.pending_since) AS asked_at,
               if({has_aliases}, ifNull(nullIf(t.person, ''), lowerUTF8(g.subject)),
                  lowerUTF8(g.subject)) AS person
        FROM (
            SELECT subject, role, status, revoked_at, pending_role, pending_since, granted_at,
                   granted_by
            FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND revoked_at IS NULL
        ) AS g
        LEFT JOIN (
            SELECT spelling, min(handle) AS person
            FROM (
                SELECT lowerUTF8(owner_email) AS handle,
                       arrayJoin([handle, lowerUTF8(contact_email)]) AS spelling
                FROM loom.namespaces FINAL
                WHERE org = {org:String} AND ifNull(active, true) = true AND contact_email != ''
                  AND {verified}
            )
            -- Only spellings that ever held a grant here (a superset of the live ones
            -- needs no FINAL): the map then grows with the namespace, not the org.
            WHERE spelling IN (
                SELECT lowerUTF8(subject) FROM loom.namespace_grants
                WHERE org = {org:String} AND namespace = {ns:String}
            )
            GROUP BY spelling
        ) AS t ON t.spelling = lowerUTF8(g.subject)
    )
    GROUP BY person
    HAVING {first:Bool} OR (head.5, head.1) < (toDateTime({at:String}, 'UTC'), {sub:String})
    ORDER BY head.5 DESC, head.1 DESC
    LIMIT {lim:UInt32}
)
ORDER BY granted_at DESC, subject DESC
"""
_PEOPLE_PAGE = (
    _PEOPLE_PAGE.replace("{live}", LIVE_GRANT)
    .replace("{rank}", GRANT_RANK_SQL)
    .replace("{rank_asked}", grant_rank_sql("asked"))
    .replace("{has_aliases}", has_aliases_sql("g.subject"))
    .replace("{verified}", VERIFIED_EMAIL_SQL)
)


async def active_on(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """(subject, role) of every active grant on (org, namespace), with no limit."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT subject, role FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND revoked_at IS NULL AND status = 'active'
        ORDER BY subject
        """,
        params={"org": org, "ns": namespace},
    )


async def people_page(
    org: str,
    namespace: str,
    *,
    after: tuple[datetime, str] | None,
    limit: int,
    ch: _CHClient | None = None,
) -> list[GrantPerson]:
    """At most `limit` people below `after`, ordered by their newest grant.

    Fold all live spellings before the keyset so no page splits a person.
    """
    ch = ch or _default_client()
    at, sub = after if after is not None else (datetime(1970, 1, 1), "")
    rows = await ch.query_all(
        _PEOPLE_PAGE,
        params={
            "org": org,
            "ns": namespace,
            "first": after is None,
            # UTC text, read back with toDateTime(…, 'UTC'): the driver binds a datetime
            # in its own zone, and DateTime has no sub-second part.
            "at": to_naive_utc(at).strftime("%Y-%m-%d %H:%M:%S"),
            "sub": sub,
            "lim": int(limit),
        },
    )
    return [GrantPerson.from_row(r) for r in rows]


# Grants that open a collection to more people than they name: the org (`everyone`, `*`),
# a group, and an agent (every end user signed in under it). With service accounts, they
# are the subjects that are not one person; `user:<id>` is an end user, so a person.
_SHARED_WIDER = (
    f"lowerUTF8(subject) IN ('{EVERYONE}', '*') OR startsWith(lowerUTF8(subject), '{GROUP}') "
    f"OR startsWith(lowerUTF8(subject), '{AGENT}')"
)
_NOT_A_PERSON = f"{_SHARED_WIDER} OR startsWith(lowerUTF8(subject), '{SERVICE_ACCOUNT}')"


# `_PEOPLE_PAGE` over a collection's lineage (it and its ancestors): a parent's grant
# reaches every child, so one person's grants anywhere on it fold into one row.
_LINEAGE_PEOPLE = _PEOPLE_PAGE.replace(
    "namespace = {ns:String}", "namespace IN {lineage:Array(String)}"
)


async def member_count(
    org: str, lineage: list[str], *, ch: _CHClient | None = None
) -> tuple[int, int]:
    """Returns `(people, shared)`. `people` is the people holding an active grant on any
    namespace of `lineage` (a namespace and, for a collection, its ancestors), folded as
    `people_page` folds them; `shared` is the active grants to everyone, a group or an
    agent, whose members are not listed. A pending request alone is not counted."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT countIf(status = 'active' AND NOT ({_NOT_A_PERSON})) AS people, "
        f"countIf(status = 'active' AND ({_SHARED_WIDER})) AS shared "
        f"FROM ({_LINEAGE_PEOPLE})",
        # The whole folded set: no keyset (`first`), no limit.
        params={
            "org": org,
            "lineage": lineage,
            "first": True,
            "at": "1970-01-01 00:00:00",
            "sub": "",
            "lim": 2**32 - 1,
        },
    )
    return (int(row["people"]), int(row["shared"])) if row else (0, 0)


async def find_active_grant(
    org: str,
    namespace: str,
    subject: str,
    *,
    ch: _CHClient | None = None,
) -> GrantState | None:
    """Look up the current ACTIVE grant for (org, ns, subject), if any.
    Pending grants are intentionally excluded — they don't authorize
    action yet."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT role, granted_by, status, pending_role, pending_since
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND subject = {sub:String} AND {live}
        LIMIT 1
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "ns": namespace, "sub": subject},
    )
    return None if row is None else GrantState.from_row(row)


async def live_rows_of(
    org: str, subject: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Every live row naming `subject` in `org`, whole, for a rewrite."""
    from server.core.ch import own_write_reads

    ch = ch or _default_client()
    with own_write_reads():
        return await ch.query_all(
            """
            SELECT * FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND subject = {sub:String} AND revoked_at IS NULL
            """,
            params={"org": org, "sub": subject},
        )


async def newest_row(
    org: str, namespace: str, subject: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """The newest row of one grant, whole, for a rewrite under its claim."""
    from server.core.ch import own_write_reads

    ch = ch or _default_client()
    with own_write_reads():
        return await ch.query_one(
            """
            SELECT * FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND subject = {sub:String}
            """,
            params={"org": org, "ns": namespace, "sub": subject},
        )


async def rows_granted_by(
    org: str, granter: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Every newest row `granter` issued in `org`, whole, for a rewrite."""
    from server.core.ch import own_write_reads

    ch = ch or _default_client()
    with own_write_reads():
        return await ch.query_all(
            """
            SELECT * FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND granted_by = {by:String}
            """,
            params={"org": org, "by": granter},
        )


async def find_any_grant(
    org: str,
    namespace: str,
    subject: str,
    *,
    ch: _CHClient | None = None,
) -> GrantState | None:
    """Look up ANY non-revoked grant — active OR pending. Used by the
    join handler to detect already-requested-but-not-yet-approved
    self-joins so they aren't duplicated."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT role, granted_by, status, pending_role, pending_since
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND subject = {sub:String} AND revoked_at IS NULL
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "sub": subject},
    )
    return None if row is None else GrantState.from_row(row)


async def stored_spellings(
    org: str,
    namespace: str,
    spellings: list[str],
    *,
    rows: Literal["live", "active", "all"] = "live",
    ch: _CHClient | None = None,
) -> list[str]:
    """The subjects the rows on (org, namespace) are stored under that match one of
    `spellings` in any case, as the gate matches them. `rows`: "live" reads the
    rows not revoked (a pending request too, as `find_any_grant` reads them),
    "active" the ones that admit, "all" every row ever written."""
    if not spellings:
        return []
    ch = ch or _default_client()
    live_only = {"live": "AND revoked_at IS NULL", "active": f"AND {LIVE_GRANT}", "all": ""}[rows]
    found = await ch.query_all(
        f"""
        SELECT DISTINCT subject FROM loom.namespace_grants FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          -- both sides lowered by ClickHouse, as the grant map lowers them
          AND has(arrayMap(s -> lower(s), {{subs:Array(String)}}), lower(subject))
          {live_only}
        ORDER BY subject
        LIMIT 64
        """,
        params={"org": org, "ns": namespace, "subs": sorted(set(spellings))},
    )
    return [str(r["subject"]) for r in found]


async def count_active_admins(
    org: str,
    namespace: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Live admin grants on (org, namespace). A pending admin request is not an admin: it
    neither keeps a namespace owned nor blocks an operator adopting an orphaned one."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT count() AS n FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND role = 'admin' AND {live}
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "ns": namespace},
    )
    return int((row or {}).get("n") or 0)


async def ranked_live_grants(
    org: str, subjects: list[str], *, limit: int = 200, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """`subjects`' live grants in `org` (namespace, role), highest role
    first, then the most recent. A subject matches whatever its case, as
    at the gate (`authz.grant_role_for`)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT namespace, role
        FROM (SELECT * FROM loom.namespace_grants FINAL) AS g
        WHERE g.org = {org:String}
          AND lower(g.subject) IN {subs:Array(String)}
          AND {live}
        ORDER BY {rank} DESC, g.granted_at DESC
        LIMIT {lim:UInt32}
        """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
        params={"org": org, "subs": [s.lower() for s in subjects], "lim": int(limit)},
    )
    return list(rows or [])
