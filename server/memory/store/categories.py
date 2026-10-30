"""loom.categories and loom.memory_categories: resolving a namespace's registered
category names and linking memory keys to them.

`loom.categories` is `ReplacingMergeTree(version)` keyed `(org, namespace, name)`,
so a live name resolves to one id. `loom.memory_categories` is
`ReplacingMergeTree(created_at)` keyed `(org, namespace, memory_key, category_id)`,
so linking a key again replaces its link. memory.assign_category links through here;
namespace.register_category writes the registry.
"""

from __future__ import annotations

from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client, query_all_sliced


async def registered_ids(
    org: str, namespace: str, names: list[str], *, ch: _CHClient | None = None
) -> dict[str, str]:
    """Name → category_id of each of `names` the namespace has registered and not
    tombstoned; a name it has not registered is absent. No names, no statement."""
    ch = ch or _default_client()
    rows = await query_all_sliced(
        ch,
        """
        SELECT name, category_id FROM loom.categories FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND name IN {names:Array(String)} AND tombstoned_at IS NULL
        """,
        {"org": org, "ns": namespace, "names": sorted(set(names))},
        "names",
    )
    return {str(r["name"]): str(r["category_id"]) for r in rows}


async def link(
    org: str,
    namespace: str,
    links: list[tuple[str, str]],
    *,
    tombstone: bool = False,
    ch: _CHClient | None = None,
) -> None:
    """One loom.memory_categories row per `(memory_key, category_id)` in `links`; with
    `tombstone`, each link is rescinded. The key is stored as given: no live row is
    required under it."""
    if not links:
        return
    ch = ch or _default_client()
    now = naive_utc_now()
    await ch.insert(
        "loom.memory_categories",
        [
            {
                "org": org,
                "namespace": namespace,
                "memory_key": memory_key,
                "category_id": category_id,
                "created_at": now,
                "tombstoned_at": now if tombstone else None,
            }
            for memory_key, category_id in links
        ],
    )
