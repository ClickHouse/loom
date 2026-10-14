"""What a `namespace_scope=all_mine` search reads.

The namespace the call addressed, first, and up to
`memory_search_fanout_max_namespaces` more of the caller's own namespaces
(`resolve_my_namespaces`, narrowed to the kinds a fan-out sweeps). The
transport authorized the addressed namespace, and a fan-out only widens a
search, so that namespace is read first and no earlier tier can end the search
before it. Code and QA indexes are readable namespaces too, but they are
reached by name or by their own tools, never swept for a personal fact.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.auth import context
from server.auth.default_namespace import auth_off_subject
from server.auth.directory import directory
from server.settings import settings


@dataclass(frozen=True)
class Fanout:
    """The caller's namespaces an all_mine search may read in `org`, before the cap."""

    org: str
    namespaces: tuple[str, ...]
    cap: int
    # The order is a priority (a scoped token's), read as strict tiers.
    ordered: bool = False
    # The public ones, which the cap drops before any private one. Empty when
    # the order is a priority or every namespace fits under the cap.
    public: frozenset[str] = frozenset()

    def reads(self, namespace: str) -> tuple[list[str], list[str]]:
        """(read, skipped) for a search addressed to `namespace`: it first, then
        up to `cap` of the others, read in their own order. When the cap cuts,
        it drops public namespaces before private ones; skipped are the ones
        left out."""
        others = [ns for ns in self.namespaces if ns != namespace]
        keep = set(sorted(others, key=lambda ns: ns in self.public)[: self.cap])
        read = [ns for ns in others if ns in keep]
        return [namespace, *read], [ns for ns in others if ns not in keep]


async def namespaces_to_search(
    org: str, namespace: str, namespace_scope: str | None
) -> tuple[list[str], list[str]]:
    """(read, skipped) for one search leg: the addressed `namespace`, and
    under `all_mine` the caller's fan-out besides."""
    if namespace_scope != "all_mine":
        return [namespace], []
    return (await resolve_fanout(org)).reads(namespace)


async def resolve_fanout(org: str) -> Fanout:
    """The caller's namespaces of the fan-out kinds, in their order, which is a
    priority only when `ordered` (a scoped token). Memoised per bound principal
    and org."""
    cached = context.fanout_memo.get()
    if cached is not None and cached.org == org:
        return cached
    # With auth off there is no token, but the single user's grants still say
    # which namespaces are theirs. With auth on, no token means no namespaces.
    auth = context.current_auth()
    subject = auth_off_subject() if auth is None else None
    readable = await context.resolve_my_namespaces(org, subject)
    # A scoped or delegated claim may repeat a namespace; it is read once.
    mine = list(dict.fromkeys(await directory().fanout_namespaces(org, readable)))
    cap = settings.memory_search_fanout_max_namespaces
    ordered = auth is not None and auth.has_ns_roles()
    public: set[str] = set()
    if len(mine) > cap and not ordered:
        # Only a scoped token's order is a priority. For anyone else the cut
        # keeps the private namespaces, the ones the search reads first.
        public = await directory().public_namespaces(org, mine)
    fanout = Fanout(
        org=org, namespaces=tuple(mine), cap=cap, ordered=ordered, public=frozenset(public)
    )
    context.fanout_memo.set(fanout)
    return fanout
