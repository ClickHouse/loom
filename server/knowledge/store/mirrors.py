"""The mirror registry, `loom.knowledge_mirrors`: which `acl-<24 hex>` namespace holds a
namespace's mirrored documents (docs/architecture/document-sources.md, The mirror)."""

from __future__ import annotations

import secrets

from server.core.repo_base import _CHClient, _default_client

TABLE = "loom.knowledge_mirrors"


def new_mirror_name() -> str:
    """A fresh mirror name: `acl-` and 24 random hex digits, which no token can name."""
    return "acl-" + secrets.token_hex(12)


async def mirror_of(org: str, namespace: str, *, ch: _CHClient | None = None) -> str:
    """The namespace's mirror, or "" when it has none."""
    found = await mirrors_of(org, [namespace], ch=ch)
    return found.get(namespace, "")


async def mirrors_of(
    org: str, namespaces: list[str], *, ch: _CHClient | None = None
) -> dict[str, str]:
    """Each of `namespaces` that has a mirror, with its mirror's name."""
    if not namespaces:
        return {}
    rows = await (ch or _default_client()).query_all(
        f"SELECT namespace, mirror FROM {TABLE} FINAL "
        "WHERE org = {org:String} AND namespace IN {namespaces:Array(String)} AND NOT deleted",
        params={"org": org, "namespaces": list(namespaces)},
    )
    return {str(r["namespace"]): str(r["mirror"]) for r in rows}


async def ensure_mirror(org: str, namespace: str, *, ch: _CHClient | None = None) -> str:
    """The namespace's mirror, made now if it has none. The caller holds the namespace's
    source lease, so two writers never make two mirrors for one namespace."""
    ch = ch or _default_client()
    mirror = await mirror_of(org, namespace, ch=ch)
    if mirror:
        return mirror
    mirror = new_mirror_name()
    await ch.insert(TABLE, [{"org": org, "namespace": namespace, "mirror": mirror}])
    return mirror


async def forget_mirrors(org: str, mirrors: dict[str, str], *, ch: _CHClient | None = None) -> None:
    """Write each namespace's mirror row `deleted`: its rows are erased, and the name
    registered again gets a new mirror."""
    if mirrors:
        rows = [
            {"org": org, "namespace": ns, "mirror": mirror, "deleted": True}
            for ns, mirror in mirrors.items()
        ]
        await (ch or _default_client()).insert(TABLE, rows)


__all__ = ["TABLE", "ensure_mirror", "forget_mirrors", "mirror_of", "mirrors_of", "new_mirror_name"]
