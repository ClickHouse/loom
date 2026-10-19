"""Collections: namespaces read and written only through grants, inherited down
a tree (docs/architecture/sharing.md).

A request's principals are its subject, the groups that list it, and the agent
a gateway asserted. On a collection, the best role any of them holds on the
collection or on one of its ancestors decides; nothing else does. A token's
namespace claim or an org admin role reaches no collection. A child only adds
to what its parents grant: to narrow, it is detached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from server.core import grant_cache
from server.operations.base import Role

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo

log = logging.getLogger(__name__)

# Ancestors a collection inherits from, at most; `collection.move` refuses a
# deeper tree.
MAX_DEPTH = 8
# A collection is known by its name, so the gate needs no read to tell one,
# and nothing that fails to load can let another rule admit one. The prefixes
# are reserved: `namespace.register` refuses them.
COLLECTION_PREFIX = "col-"
PERSONAL_PREFIX = "col-me-"

_ROLE_BY_RANK = {1: Role.VIEWER, 2: Role.WRITER, 3: Role.ADMIN}


@dataclass(frozen=True)
class Tree:
    """An org's collections: each one's parent ('' for a root), and the personal ones."""

    parent: dict[str, str] = field(default_factory=dict)
    personal: frozenset[str] = frozenset()

    def lineage(self, namespace: str) -> list[str]:
        """`namespace` and its ancestors, nearest first, at most MAX_DEPTH of
        them. A cycle stops the walk where it repeats."""
        out = [namespace]
        node = self.parent.get(namespace, "")
        while node and node not in out and len(out) <= MAX_DEPTH:
            out.append(node)
            node = self.parent.get(node, "")
        return out

    @cached_property
    def children(self) -> dict[str, list[str]]:
        kids: dict[str, list[str]] = {}
        for child, parent in self.parent.items():
            if parent:
                kids.setdefault(parent, []).append(child)
        return kids

    def height(self, namespace: str) -> int:
        """Levels of collections below `namespace` (0 for a leaf)."""
        level, depth, seen = [namespace], 0, {namespace}
        while level := [c for ns in level for c in self.children.get(ns, ()) if c not in seen]:
            seen.update(level)
            depth += 1
        return depth

    def with_descendants(self, roots: set[str]) -> set[str]:
        """`roots` and every collection below one of them, MAX_DEPTH levels down."""
        seen = set(roots)
        level = list(roots)
        for _ in range(MAX_DEPTH):
            level = [c for ns in level for c in self.children.get(ns, ()) if c not in seen]
            if not level:
                break
            seen.update(level)
        return seen


async def tree(org: str) -> Tree:
    """The org's collection tree, cached with its grants."""
    return await grant_cache.org_value("collections", org, load_tree)


async def load_tree(org: str) -> Tree:
    """The org's collection tree as stored now (`collection.move` reads it
    under its claim)."""
    rows = await tree_rows(org)
    return Tree(
        parent={str(r["namespace"]): str(r["parent"] or "") for r in rows},
        personal=frozenset(str(r["namespace"]) for r in rows if r["personal"]),
    )


async def tree_rows(org: str) -> list[dict[str, Any]]:
    from server.core.ch import get_client, own_write_reads

    with own_write_reads():
        return await get_client().query_all(
            """
            SELECT namespace, parent, personal
            FROM loom.collections FINAL
            WHERE org = {org:String} AND NOT deleted
            """,
            params={"org": org},
        )


async def groups_of(org: str, principal: str) -> list[str]:
    """The `group:<id>` principals whose members include `principal`."""
    members = await grant_cache.org_value("groups", org, _load_groups)
    return members.get(principal.lower(), [])


async def _load_groups(org: str) -> dict[str, list[str]]:
    from server.core.ch import get_client, own_write_reads

    with own_write_reads():
        rows = await get_client().query_all(
            """
            SELECT group_id, members
            FROM loom.principal_groups FINAL
            WHERE org = {org:String} AND NOT deleted
            """,
            params={"org": org},
        )
    out: dict[str, list[str]] = {}
    for r in rows:
        for member in r["members"] or []:
            out.setdefault(str(member).lower(), []).append(f"group:{r['group_id']}")
    return out


async def principals(auth: AuthInfo, org: str) -> list[str]:
    """Every grant subject this request holds: its own, its groups', its agent's."""
    out = [auth.subject, *await groups_of(org, auth.subject)]
    if auth.agent_principal:
        out.append(auth.agent_principal)
    return out


def is_collection(namespace: str) -> bool:
    return namespace.startswith(COLLECTION_PREFIX)


def is_personal(namespace: str) -> bool:
    return namespace.startswith(PERSONAL_PREFIX)


async def role(auth: AuthInfo, org: str, namespace: str) -> Role | None:
    """The best role any of the request's principals holds on `namespace` or
    one of its ancestors."""
    lineage = (await tree(org)).lineage(namespace)
    best = 0
    for principal in await principals(auth, org):
        grants = await grant_cache.grant_map(org, principal)
        best = max(best, *(grants.rank(ns) or 0 for ns in lineage))
    return _ROLE_BY_RANK.get(best)


async def readable(auth: AuthInfo | None, org: str) -> list[str]:
    """Every collection of `org` the request may read, sorted; none when the
    tree or a grant cannot be read."""
    from server.auth.authz import may_act_in_org
    from server.auth.fail_closed import note_unreadable

    if auth is None or not may_act_in_org(auth, org):
        return []
    try:
        collections = await tree(org)
        granted: set[str] = set()
        for principal in await principals(auth, org):
            grants = await grant_cache.grant_map(org, principal)
            granted.update(ns for ns, rank in grants.ranks.items() if rank and is_collection(ns))
    except Exception as e:  # noqa: BLE001 — what cannot be read is not readable
        log.warning("readable collections of %s in %s not resolved: %s", auth.subject, org, e)
        note_unreadable()
        return []
    if auth.has_role(Role.PLATFORM_ADMIN):
        return sorted(collections.parent)
    return sorted(collections.with_descendants(granted))


async def hidden_from(auth: AuthInfo, org: str) -> list[str] | None:
    """The collections of `org` the request may not read; None when the tree
    cannot be read, so the caller hides everything."""
    try:
        names = (await tree(org)).parent
    except Exception as e:  # noqa: BLE001 — the caller fails closed on None
        log.warning("collection tree of %s not readable: %s", org, e)
        return None
    can_read = set(await readable(auth, org))
    return sorted(ns for ns in names if ns not in can_read)


async def check(auth: AuthInfo, required_role: Role, org: str, namespace: str) -> None:
    """The gate for a collection: PermissionError unless a principal of this
    request holds `required_role` there, directly or through an ancestor. A
    platform admin (an operator's erasure or export) passes."""
    from server.auth.authz import may_act_in_org
    from server.auth.fail_closed import note_unreadable

    if auth.has_role(Role.PLATFORM_ADMIN):
        return
    refusal = PermissionError(f"principal {auth.subject!r} cannot access {org}/{namespace}")
    if not may_act_in_org(auth, org):
        raise refusal
    try:
        granted = await role(auth, org, namespace)
    except Exception as e:
        note_unreadable()
        raise refusal from e
    if granted is None or granted.value < required_role.value:
        raise refusal
