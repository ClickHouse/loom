"""The mirror registry, `loom.knowledge_mirrors`: which `acl-<24 hex>` namespaces hold a
namespace's mirrored documents (docs/architecture/document-sources.md, The mirror)."""

from __future__ import annotations

from datetime import datetime

from server.core.repo_base import _CHClient, _default_client

TABLE = "loom.knowledge_mirrors"
LISTINGS = "loom.knowledge_source_listings"


async def mirrors_of(
    org: str, namespaces: list[str], *, live_only: bool = False, ch: _CHClient | None = None
) -> dict[str, str]:
    """Each mirror the namespaces have had, with its namespace, in name order (an erasure
    step binds them, and its mutation tag must not change): every one, written `deleted`
    or not, or with `live_only` those not yet written `deleted`."""
    if not namespaces:
        return {}
    live = " AND NOT deleted" if live_only else ""
    rows = await (ch or _default_client()).query_all(
        f"SELECT mirror, namespace FROM {TABLE} FINAL "
        "WHERE org = {org:String} AND namespace IN {namespaces:Array(String)}"
        f"{live} ORDER BY mirror",
        params={"org": org, "namespaces": namespaces},
    )
    return {str(r["mirror"]): str(r["namespace"]) for r in rows}


async def forget_mirrors(org: str, mirrors: dict[str, str], *, ch: _CHClient | None = None) -> None:
    """Write each mirror `deleted`, so its namespace registered again gets a new one."""
    if mirrors:
        rows = [
            {"org": org, "namespace": ns, "mirror": mirror, "deleted": True}
            for mirror, ns in mirrors.items()
        ]
        await (ch or _default_client()).insert(TABLE, rows)


async def add_mirror(org: str, namespace: str, mirror: str, *, ch: _CHClient | None = None) -> None:
    await (ch or _default_client()).insert(
        TABLE, [{"org": org, "namespace": namespace, "mirror": mirror, "deleted": False}]
    )


async def record_listing(
    org: str,
    mirror: str,
    source_id: str,
    name: str,
    started_at: datetime,
    *,
    ch: _CHClient | None = None,
) -> None:
    """The start of a mirrored source's last complete listing, written once every list
    that listing changed is written."""
    row = {
        "org": org,
        "namespace": mirror,
        "source_id": source_id,
        "name": name,
        "started_at": started_at,
    }
    await (ch or _default_client()).insert(LISTINGS, [row])


__all__ = [
    "LISTINGS",
    "TABLE",
    "add_mirror",
    "forget_mirrors",
    "mirrors_of",
    "record_listing",
]
