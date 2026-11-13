"""Every statement over loom.search_misses and loom.knowledge_gaps.

Both are plain MergeTrees sorted by (org, namespace, ...), and every read but the
worker's two scans (`namespaces_to_build`, `seconds_since_last_build`) filters on that
prefix. A gap read takes the namespace's latest finished build: the latest with its
marker row (empty `gap_id`, written last and never served). Older builds expire with the
table's TTL.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client

MISSES = "loom.search_misses"
GAPS = "loom.knowledge_gaps"
# Days of misses a pass groups, and days a build is served (migration 095's TTL on
# knowledge_gaps). Misses live 16 days, so a miss outlives every build that counted it.
MISS_DAYS = 7  # not a setting: tied to the tables' TTLs
BUILD_DAYS = 7  # not a setting: knowledge_gaps' TTL


async def insert_miss(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """One miss, returning once the row is visible, so the erasure fence read after it
    sees what an erasure's step would. Off the request's path."""
    await (ch or _default_client()).insert(
        MISSES, [row], settings={"async_insert": 1, "wait_for_async_insert": 1}
    )


async def delete_miss(
    org: str, namespace: str, miss_id: str, *, ch: _CHClient | None = None
) -> None:
    """Drop one miss an erasure fence caught (a lightweight delete). It waits for the
    delete, as `delete_build` does, so the erasure's verify census that may follow does
    not count the row."""
    await (ch or _default_client()).execute(
        f"DELETE FROM {MISSES} WHERE org = {{org:String}} AND namespace = {{ns:String}}"
        " AND miss_id = {id:String}",
        {"org": org, "ns": namespace, "id": miss_id},
    )


async def namespaces_to_build(
    days: int, limit: int, *, ch: _CHClient | None = None
) -> list[tuple[str, str]]:
    """The (org, namespace) pairs with a miss in the last `days` days or whose latest
    finished build still holds a gap, at most `limit`, those with the most misses first.
    A namespace whose misses left the window gets passes until an empty build replaces
    its gaps, and then drops out."""
    rows = await (ch or _default_client()).query_all(
        f"""
        SELECT org, namespace, sum(n) AS misses
        FROM (
            SELECT org, namespace, count() AS n FROM {MISSES}
            WHERE ts > now() - toIntervalDay({{days:UInt32}})
            GROUP BY org, namespace
            UNION ALL
            SELECT DISTINCT org, namespace, toUInt64(0) AS n FROM {GAPS}
            WHERE gap_id != '' AND (org, namespace, build) IN (
                SELECT org, namespace, max(build) FROM {GAPS} WHERE gap_id = ''
                GROUP BY org, namespace
            )
        )
        GROUP BY org, namespace
        ORDER BY misses DESC, org, namespace
        LIMIT {{lim:UInt32}}
        """,
        {"days": days, "lim": limit},
    )
    return [(str(r["org"]), str(r["namespace"])) for r in rows]


async def seconds_since_last_build(*, ch: _CHClient | None = None) -> int | None:
    """Seconds since the newest finished build of any namespace; None when there is none."""
    row = await (ch or _default_client()).query_one(
        f"SELECT greatest(0, dateDiff('second', max(built_at), now())) AS age, count() AS n "
        f"FROM {GAPS} WHERE gap_id = ''"
    )
    return int(row["age"]) if row and int(row["n"]) else None


async def miss_groups(
    org: str,
    namespace: str,
    *,
    days: int,
    min_askers: int,
    limit: int,
    max_askers: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The namespace's misses of the last `days` days grouped by normalised query,
    keeping a group only when at least `min_askers` distinct askers asked it in that
    form; the `limit` with the most distinct askers, then asks, then the latest ask, each
    with its count, latest time and at most `max_askers` of its askers."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT normalized AS head,
               count() AS asks,
               toStartOfDay(max(ts)) AS last_at,
               groupUniqArray({{cap:UInt32}})(asker) AS askers
        FROM {MISSES}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND ts > now() - toIntervalDay({{days:UInt32}})
          AND normalized != ''
        GROUP BY normalized
        HAVING uniqExact(asker) >= {{k:UInt32}}
        ORDER BY uniqExact(asker) DESC, asks DESC, max(ts) DESC, head
        LIMIT {{lim:UInt32}}
        """,
        {
            "org": org,
            "ns": namespace,
            "days": days,
            "k": min_askers,
            "lim": limit,
            "cap": max_askers,
        },
    )


async def insert_build(rows: list[dict[str, Any]], *, ch: _CHClient | None = None) -> None:
    """One build of a namespace's gaps."""
    if rows:
        await (ch or _default_client()).insert(GAPS, rows)


async def latest_gaps(
    org: str, namespace: str, *, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The namespace's latest finished build: its marker row (empty `gap_id`) first, then
    at most `limit` gaps, most askers first. Empty when there is none."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT gap_id, head, samples, askers, asks, last_at, built_at
        FROM {GAPS}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND build = (
              SELECT max(build) FROM {GAPS}
              WHERE org = {{org:String}} AND namespace = {{ns:String}} AND gap_id = ''
                AND built_at > now() - toIntervalDay({{days:UInt32}})
          )
        ORDER BY gap_id != '', askers DESC, asks DESC, gap_id
        LIMIT {{lim:UInt32}} + 1
        """,
        {"org": org, "ns": namespace, "lim": limit, "days": BUILD_DAYS},
    )


async def delete_build(
    org: str, namespace: str, build: int, *, ch: _CHClient | None = None
) -> None:
    """Drop one build of the namespace's gaps (a lightweight delete): readers then take
    the build before it, or none. It waits for the delete, as `delete_miss` does: it
    drops a build an erasure raced, whose verify census may follow."""
    await (ch or _default_client()).execute(
        f"DELETE FROM {GAPS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND build = {build:UInt64}",
        {"org": org, "ns": namespace, "build": build},
    )


async def delete_misses(org: str, namespace: str, *, ch: _CHClient | None = None) -> None:
    """Drop the namespace's stored misses (a lightweight delete), for a namespace whose
    capture level is now `off`: a later consent must not count them again."""
    await (ch or _default_client()).execute(
        f"DELETE FROM {MISSES} WHERE org = {{org:String}} AND namespace = {{ns:String}}",
        {"org": org, "ns": namespace},
        settings={"lightweight_deletes_sync": 0},
    )


async def delete_builds(org: str, namespace: str, *, ch: _CHClient | None = None) -> None:
    """Drop every build of the namespace's gaps (a lightweight delete), with its misses
    for a namespace whose capture level is now `off`: a user erasure finds a namespace
    through the user's misses, so builds must not outlive them."""
    await (ch or _default_client()).execute(
        f"DELETE FROM {GAPS} WHERE org = {{org:String}} AND namespace = {{ns:String}}",
        {"org": org, "ns": namespace},
        settings={"lightweight_deletes_sync": 0},
    )
