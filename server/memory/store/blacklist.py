"""Repository for loom.blacklist ("Refuse" patterns).

Three scopes: global (org='*', namespace='*'), org-wide (org=<org>,
namespace='*'), and namespace (both set). The list-by-scope SQL has
to encode all three lookup shapes; centralizing here keeps the
predicates in one place.

Soft-delete via tombstoned_at; an ALTER UPDATE flips it. The in-process
cache and when it refreshes live in server.memory.app.blacklist_guard; the
statements it runs (the active patterns, the version watermark) are here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client


async def insert_row(
    row: dict[str, Any],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert one blacklist pattern. async_insert=0 so the next list
    sees the new row."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.blacklist",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def list_for_scope(
    org: str,
    namespace: str,
    *,
    scope: str | None = None,
    limit: int = 100,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """List blacklist patterns visible to (org, namespace).

    `scope=None` returns every pattern that applies (namespace +
    org-wide + global). Specific scope strings filter exact match.
    """
    ch = ch or _default_client()
    if scope is None:
        where = (
            "((org = {org:String} AND namespace = {ns:String}) "
            "OR (org = {org:String} AND namespace = '*') "
            "OR (org = '*' AND namespace = '*'))"
        )
    elif scope == "global":
        where = "scope = 'global'"
    elif scope == "org":
        where = "org = {org:String} AND scope = 'org'"
    else:
        where = "org = {org:String} AND namespace = {ns:String} AND scope = 'namespace'"
    return await ch.query_all(
        f"""
        SELECT pattern_id, org, namespace, pattern, pattern_type, scope,
               reason, created_by, hit_count, last_hit_at, created_at
        FROM loom.blacklist FINAL
        WHERE tombstoned_at IS NULL AND {where}
        ORDER BY created_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )


async def find_active_pattern(
    pattern_id: str,
    *,
    org: str | None = None,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """Look up a live pattern by id, optionally restricted to one org.

    Returns the row's org, namespace and scope with the id: the caller's
    authority to remove or sweep a pattern is decided from the row's own
    scope, never from which lookup found it. Global rows carry org='*';
    pass ``org="*"`` to look up only those.
    """
    ch = ch or _default_client()
    where = ["pattern_id = {pid:UUID}", "tombstoned_at IS NULL"]
    params: dict[str, Any] = {"pid": pattern_id}
    if org is not None:
        where.append("org = {org:String}")
        params["org"] = org
    return await ch.query_one(
        f"""
        SELECT pattern_id, org, namespace, scope, pattern, pattern_type
        FROM loom.blacklist FINAL
        WHERE {" AND ".join(where)}
        LIMIT 1
        """,
        params=params,
    )


async def tombstone_pattern(
    pattern_id: str,
    when: datetime,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Soft-delete via ALTER UPDATE. mutations_sync=2 so the next
    list call doesn't see the row."""
    ch = ch or _default_client()
    await ch.execute(
        """
        ALTER TABLE loom.blacklist
        UPDATE tombstoned_at = {ts:DateTime}
        WHERE pattern_id = {pid:UUID}
        SETTINGS mutations_sync=2
        """,
        params={"pid": pattern_id, "ts": when},
    )


async def active_patterns(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Every non-tombstoned pattern, oldest first, ties by id, so every replica reads
    them in one order (the cache orders them by scope precedence)."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT pattern_id, org, namespace, pattern, pattern_type, scope, reason
        FROM loom.blacklist FINAL
        WHERE tombstoned_at IS NULL
        ORDER BY created_at ASC, pattern_id ASC
        """,
    )


async def scope_regexes(
    scope: str, org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[str]:
    """The live regex patterns stored at exactly this scope."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT pattern FROM loom.blacklist FINAL
        WHERE tombstoned_at IS NULL AND pattern_type = 'regex'
          AND scope = {scope:String} AND org = {org:String} AND namespace = {ns:String}
        """,
        params={"scope": scope, "org": org, "ns": namespace},
    )
    return [str(r["pattern"]) for r in rows]


async def version_watermark(*, ch: _CHClient | None = None) -> tuple[int, int]:
    """(rows, sum of versions) over the watermark table's unmerged rows. Every
    add/remove inserts a row with a fresh version, so the pair moves on every
    change whichever writer's clock is ahead; a background merge collapsing
    rows moves it too, which only costs one extra refresh."""
    ch = ch or _default_client()
    row = await ch.query_one("SELECT count() AS n, sum(version) AS s FROM loom.blacklist_version")
    if not row:
        return (0, 0)
    return (int(row.get("n") or 0), int(row.get("s") or 0))


async def insert_version(version: int, *, updated_by: str, ch: _CHClient | None = None) -> None:
    """Append one watermark row. No read first: two concurrent bumps write two
    distinct rows."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.blacklist_version",
        [{"singleton": 0, "version": int(version), "updated_by": updated_by}],
    )
