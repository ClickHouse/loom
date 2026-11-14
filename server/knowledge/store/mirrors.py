"""The mirror registry, `loom.knowledge_mirrors`: which `acl-<24 hex>` namespaces hold a
namespace's mirrored documents (docs/architecture/document-sources.md, The mirror)."""

from __future__ import annotations

from server.core.repo_base import _CHClient, _default_client

TABLE = "loom.knowledge_mirrors"


async def mirrors_of(
    org: str, namespaces: list[str], *, ch: _CHClient | None = None
) -> dict[str, str]:
    """Every mirror the namespaces have had, written `deleted` or not, with its namespace."""
    if not namespaces:
        return {}
    rows = await (ch or _default_client()).query_all(
        f"SELECT mirror, namespace FROM {TABLE} FINAL "
        "WHERE org = {org:String} AND namespace IN {namespaces:Array(String)}",
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


__all__ = ["TABLE", "forget_mirrors", "mirrors_of"]
