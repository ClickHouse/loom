"""Collections: namespaces read and written only through grants, inherited down
a tree (docs/architecture/sharing.md).

A request's principals are its subject, the groups that list it, and the agent
a gateway asserted. On a collection, the best role any of them holds on the
collection or on one of its ancestors decides; nothing else does. A token's
namespace claim or an org admin role reaches no collection, and a scoped or
delegated token reaches one no further than its own claim (and its creator's
role) there. A child only adds to what its parents grant: to narrow, it is
detached.

A personal collection belongs to the one end user its name is derived from;
it takes no grant and sits in no tree. A `col-` name that is in no tree (deleted,
never created) admits no one. A service account reaches a collection only by its
own grants: unlike an ordinary namespace, never through an inheriting creator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from server.core import grant_cache
from server.core.principals import PERSONAL_PREFIX, USER, personal_name, principal_id
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
# What a request names its end user's personal collection by.
PERSONAL_ALIAS = "@me"

_ROLE_BY_RANK = {1: Role.VIEWER, 2: Role.WRITER, 3: Role.ADMIN}


def is_collection(namespace: str) -> bool:
    return namespace.startswith(COLLECTION_PREFIX)


def is_personal(namespace: str) -> bool:
    return namespace.startswith(PERSONAL_PREFIX)


@dataclass(frozen=True)
class Tree:
    """An org's collections, personal ones aside: each one's parent ('' for a root)."""

    parent: dict[str, str] = field(default_factory=dict)

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
    return Tree(parent={str(r["namespace"]): str(r["parent"] or "") for r in await tree_rows(org)})


async def tree_rows(org: str) -> list[dict[str, Any]]:
    from server.core.ch import get_client, own_write_reads

    with own_write_reads():
        return await get_client().query_all(
            """
            SELECT namespace, parent
            FROM loom.collections FINAL
            WHERE org = {org:String} AND NOT deleted
            """,
            params={"org": org},
        )


async def groups_of(org: str, principal: str) -> list[str]:
    """The `group:<id>` principals whose members include `principal`."""
    members = await grant_cache.org_value("groups", org, _load_groups)
    return members.get(principal, [])


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
            out.setdefault(str(member), []).append(f"group:{r['group_id']}")
    return out


async def principals(auth: AuthInfo, org: str) -> list[str]:
    """Every grant subject this request holds: its own, its groups', its agent's."""
    out = [auth.subject, *await groups_of(org, auth.subject)]
    if auth.agent_principal:
        out.append(auth.agent_principal)
    return out


def owns_personal(auth: AuthInfo, org: str, namespace: str) -> bool:
    """True when `namespace` is the personal collection of the end user `auth` runs as."""
    return bool(principal_id(auth.subject, USER)) and namespace == personal_name(org, auth.subject)


async def _best_role(org: str, lineage: list[str], subjects: list[str]) -> Role | None:
    best = 0
    for subject in subjects:
        grants = await grant_cache.grant_map(org, subject)
        best = max(best, *(grants.rank(ns) or 0 for ns in lineage))
    return _ROLE_BY_RANK.get(best)


async def role(auth: AuthInfo, org: str, namespace: str) -> Role | None:
    """The request's role on the collection `namespace`."""
    if is_personal(namespace):
        return Role.ADMIN if owns_personal(auth, org, namespace) else None
    collections = await tree(org)
    if namespace not in collections.parent:
        return None  # deleted, never created, or a `col-` name from before collections
    lineage = collections.lineage(namespace)
    granted = await _best_role(org, lineage, await principals(auth, org))
    if granted is None or not (auth.has_ns_roles() or auth.creator):
        return granted
    # A scoped or delegated token: no further than its own claim names there,
    # and a delegated one no further than its creator's role.
    from server.auth.authz import names_namespace

    if not names_namespace(auth, org, namespace):
        return None
    if auth.has_ns_roles():
        own = auth.effective_role(org, namespace)
    else:
        own = max(auth.roles, key=lambda r: r.value, default=None)
    ceilings = [granted, own]
    if auth.creator:
        ceilings.append(await _best_role(org, lineage, [auth.creator]))
    if None in ceilings:
        return None
    return min(ceilings, key=lambda r: r.value)  # type: ignore[union-attr]


async def roles(auth: AuthInfo, org: str, namespaces: list[str]) -> dict[str, Role | None]:
    """`role` for many collections, reading the tree, the principals and their
    grant maps once."""
    if auth.has_ns_roles() or auth.creator:
        return {ns: await role(auth, org, ns) for ns in namespaces}
    collections = await tree(org)
    maps = [await grant_cache.grant_map(org, p) for p in await principals(auth, org)]
    out: dict[str, Role | None] = {}
    for ns in namespaces:
        if is_personal(ns):
            out[ns] = Role.ADMIN if owns_personal(auth, org, ns) else None
            continue
        if ns not in collections.parent:
            out[ns] = None
            continue
        lineage = collections.lineage(ns)
        out[ns] = _ROLE_BY_RANK.get(max((m.rank(n) or 0 for m in maps for n in lineage), default=0))
    return out


async def readable(auth: AuthInfo | None, org: str) -> list[str]:
    """Every collection of `org` the request may read, sorted; none (but the
    user's own) when the tree or a grant cannot be read."""
    from server.auth.authz import may_act_in_org
    from server.auth.fail_closed import note_unreadable

    if auth is None or not may_act_in_org(auth, org):
        return []
    found: set[str] = set()
    if principal_id(auth.subject, USER):
        found.add(personal_name(org, auth.subject))
    try:
        collections = await tree(org)
        if auth.has_role(Role.PLATFORM_ADMIN):
            return sorted(found | set(collections.parent))
        granted: set[str] = set()
        for principal in await principals(auth, org):
            grants = await grant_cache.grant_map(org, principal)
            granted.update(
                ns for ns, rank in grants.ranks.items() if rank and ns in collections.parent
            )
        shared = collections.with_descendants(granted)
        if auth.has_ns_roles() or auth.creator:
            shared = {ns for ns in shared if await role(auth, org, ns)}
    except Exception as e:  # noqa: BLE001 — what cannot be read is not readable
        log.warning("readable collections of %s in %s not resolved: %s", auth.subject, org, e)
        note_unreadable()
        return sorted(found)
    return sorted(found | shared)


async def check(auth: AuthInfo, required_role: Role, org: str, namespace: str) -> None:
    """The gate for a collection: PermissionError unless the request holds
    `required_role` there. A platform admin (an operator's erasure or export)
    passes."""
    from server.auth.authz import may_act_in_org
    from server.auth.fail_closed import note_unreadable

    if auth.has_role(Role.PLATFORM_ADMIN):
        return
    refusal = PermissionError(f"principal {auth.subject!r} cannot access {org}/{namespace}")
    if not may_act_in_org(auth, org):
        raise refusal
    try:
        granted = await role(auth, org, namespace)
    except Exception as e:  # a tree or grant that cannot be read admits nothing: refused
        note_unreadable()
        raise refusal from e
    if granted is None or granted.value < required_role.value:
        raise refusal
