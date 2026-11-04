"""Collections: namespaces read and written only through grants, inherited down
a tree (docs/architecture/sharing.md).

A request's principals are its subject, the groups that list it, the agent an
end-user token signs in, and everyone in the org. On a collection, the best
role any of them holds on the collection or on one of its ancestors decides;
nothing else does. A token's
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

import asyncio
import logging
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from server.core import grant_cache
from server.core.errors import BACKEND_NOT_NOW
from server.core.principals import EVERYONE, PERSONAL_PREFIX, USER, personal_name, principal_id
from server.operations.base import Role, rank_role

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo

log = logging.getLogger(__name__)

# Ancestors a collection inherits from, at most; `collection.move` refuses a
# deeper tree.
MAX_DEPTH = 8  # not a setting: a lower value would cut inheritance in trees already stored
# A collection is known by its name, so the gate needs no read to tell one,
# and nothing that fails to load can let another rule admit one. The prefixes
# are reserved: `namespace.register` and `namespace.restore` refuse them.
COLLECTION_PREFIX = "col-"
# What a request names its end user's personal collection by.
PERSONAL_ALIAS = "@me"


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
    return await grant_cache.org_value(grant_cache.COLLECTIONS, org, load_tree)


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
    members = await grant_cache.org_value(grant_cache.GROUPS, org, _load_groups)
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
    """Every grant subject this request holds: its own, its groups', its
    agent's, and `everyone` (the caller checked it may act in `org`)."""
    out = [auth.subject, *await groups_of(org, auth.subject), EVERYONE]
    if auth.agent_principal:
        out.append(auth.agent_principal)
    return out


def owns_personal(auth: AuthInfo, org: str, namespace: str) -> bool:
    """True when `namespace` is the personal collection of the end user `auth` runs as."""
    return bool(principal_id(auth.subject, USER)) and namespace == personal_name(org, auth.subject)


def _best(maps: list[grant_cache.GrantMap], lineage: list[str]) -> Role | None:
    """The highest role any of `maps` grants on a collection or an ancestor."""
    return rank_role(max((m.rank(ns) or 0 for m in maps for ns in lineage), default=0))


def _claim_limits(auth: AuthInfo, org: str, namespace: str) -> list[tuple[Role | None, str]]:
    """Each limit the token's own claim sets on `namespace`, whatever its grants, with what
    the claim must say to lift it: `viewer` outside its `ns_write` set; for a scoped or
    delegated token, nothing (None) unless the claim names `namespace` (a `*` does not), and
    no more than the claim's own role there (None for a role no grant carries)."""
    limits: list[tuple[Role | None, str]] = []
    if auth.write_namespaces and not auth.can_write(org, namespace):
        limits.append((Role.VIEWER, "this token's write set (ns_write) must include it"))
    if not (auth.has_ns_roles() or auth.creator):
        return limits
    from server.auth.authz import names_namespace  # authz imports this module

    if not names_namespace(auth, org, namespace):
        limits.append((None, "this token's claim must name it"))
    elif auth.has_ns_roles():
        limits.append(
            (
                auth.effective_role(org, namespace),
                "this token's claim must name it with at least {role}",
            )
        )
    else:
        own = max(auth.roles, key=lambda r: r.value, default=None)
        limits.append((own, "this token's claim must carry at least {role}"))
    return limits


def _claim_ceiling(auth: AuthInfo, org: str, namespace: str) -> Role | None:
    """The most the token's own claim lets it hold on `namespace` (`_claim_limits`): None
    for nothing, `admin` when the claim sets no limit."""
    ceilings = [limit for limit, _ in _claim_limits(auth, org, namespace)]
    if None in ceilings:
        return None
    return min([Role.ADMIN, *ceilings], key=lambda r: r.value)  # type: ignore[union-attr]


def _capped(
    auth: AuthInfo, org: str, namespace: str, granted: Role | None, creator: Role | None
) -> Role | None:
    """`granted`, no further than the token's own claim allows (`_claim_ceiling`), and for
    a delegated token no further than its creator's role there (`creator`)."""
    if granted is None:
        return None
    ceilings = [granted, _claim_ceiling(auth, org, namespace), *([creator] if auth.creator else [])]
    if None in ceilings:
        return None
    return min(ceilings, key=lambda r: r.value)  # type: ignore[union-attr]


async def role(auth: AuthInfo, org: str, namespace: str) -> Role | None:
    """The request's role on the collection `namespace`."""
    return (await roles(auth, org, [namespace]))[namespace]


async def roles(auth: AuthInfo, org: str, namespaces: list[str]) -> dict[str, Role | None]:
    """The request's role on each collection of `namespaces`, reading the tree,
    the principals and their grant maps once. A personal collection needs no
    read: its name says whose it is. A platform admin holds admin on every one
    (`check` admits it)."""
    if auth.has_role(Role.PLATFORM_ADMIN):
        return dict.fromkeys(namespaces, Role.ADMIN)
    out = {
        ns: (Role.ADMIN if owns_personal(auth, org, ns) else None)
        for ns in namespaces
        if is_personal(ns)
    }
    shared = [ns for ns in namespaces if ns not in out]
    if not shared:
        return out
    collections = await tree(org)
    maps = await _grant_maps(org, await principals(auth, org))
    creator = await _grant_maps(org, [auth.creator]) if auth.creator else []
    for ns in shared:
        if ns not in collections.parent:
            out[ns] = None  # a `col-` name not in the tree admits only a platform admin
            continue
        lineage = collections.lineage(ns)
        out[ns] = _capped(auth, org, ns, _best(maps, lineage), _best(creator, lineage))
    return out


# Grant-map reads one request runs at once: each holds a ClickHouse connection.
# not a setting: one request's fan-out; the ClickHouse pool bounds the total
_GRANT_READS_AT_ONCE = 8


async def _grant_maps(org: str, subjects: list[str]) -> list[grant_cache.GrantMap]:
    """Each subject's grant map, read a few side by side: a user in many groups
    waits a few round trips after a grant change, not one per group, and leaves
    the pool to other requests."""
    maps: list[grant_cache.GrantMap] = []
    for i in range(0, len(subjects), _GRANT_READS_AT_ONCE):
        batch = subjects[i : i + _GRANT_READS_AT_ONCE]
        maps.extend(await asyncio.gather(*(grant_cache.grant_map(org, s) for s in batch)))
    return maps


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
        for grants in await _grant_maps(org, await principals(auth, org)):
            granted.update(
                ns for ns, rank in grants.ranks.items() if rank and ns in collections.parent
            )
        shared = collections.with_descendants(granted)
        if auth.has_ns_roles() or auth.creator:
            capped = await roles(auth, org, sorted(shared))
            shared = {ns for ns, r in capped.items() if r is not None}
    except BACKEND_NOT_NOW:
        raise  # an outage answers 503, as for any namespace: never "every share is gone"
    except Exception as e:  # noqa: BLE001 — what cannot be read is not readable
        log.warning("readable collections of %s in %s not resolved: %s", auth.subject, org, e)
        note_unreadable()
        return sorted(found)
    return sorted(found | shared)


def claim_cap(auth: AuthInfo, org: str, namespace: str, required_role: Role) -> str | None:
    """What the token's own claim must say when it, rather than a missing grant, keeps the
    token below `required_role` on `namespace` (`_claim_limits`, the limits the gate
    applies): every limit that does, so one re-mint lifts them all; None when none does.
    Read from the token alone, so it says nothing about the collection."""
    role = required_role.name.lower()
    needs = [
        why.format(role=role)
        for limit, why in _claim_limits(auth, org, namespace)
        if limit is None or limit.value < required_role.value
    ]
    return ", and ".join(needs) or None


async def check(auth: AuthInfo, required_role: Role, org: str, namespace: str) -> None:
    """The gate for a collection: PermissionError unless the request holds
    `required_role` there. A platform admin (an operator's erasure or export)
    passes."""
    from server.auth.authz import may_act_in_org
    from server.auth.fail_closed import note_unreadable

    if auth.has_role(Role.PLATFORM_ADMIN):
        return
    # The same words whether or not the collection exists, so they confirm nothing.
    bare = PermissionError(f"principal {auth.subject!r} cannot access {org}/{namespace}")
    # Another org's caller, and someone else's personal collection (which takes no grant),
    # get the short refusal: the advice below could not work for them.
    if not may_act_in_org(auth, org):
        raise bare
    try:
        have = await role(auth, org, namespace)
    except BACKEND_NOT_NOW:
        raise  # an outage answers 503, as for any namespace, so a caller retries
    except Exception as e:  # a tree or grant that cannot be read admits nothing: refused
        note_unreadable()
        # Unverified, not refused for a missing grant: the short words, so a caller that holds
        # one retries rather than asking for it.
        raise bare from e
    if have is not None and have.value >= required_role.value:
        return
    if is_personal(namespace):
        raise bare
    role_name = required_role.name.lower()
    # Read from the token alone, never from the grants: what the caller's grants reach on a
    # collection it cannot see would confirm the collection exists. A token its claim caps is
    # told both what it needs: a claim that names it, and a grant for the principal the
    # grants are read for (the token's own subject; a new token is capped to it).
    grant = f"a grant of at least {role_name} on it or a collection above it"
    needs = claim_cap(auth, org, namespace, required_role)
    notes = (
        [needs, f"{auth.subject!r} must hold {grant} (directly, through a group or to everyone)"]
        if needs
        else [f"a collection needs {grant}"]
    )
    if auth.creator:
        notes.append(f"its creator must hold at least {role_name} there too")
    raise PermissionError(
        # The gate knows no transport: the MCP screen says how its connection reaches the op.
        f"{bare}: {', and '.join(notes)}; collection.list (or the dashboard's Library) lists "
        "the ones you can read"
    )
