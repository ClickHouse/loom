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
from typing import NamedTuple

from server.auth import context
from server.auth.default_namespace import auth_off_subject
from server.auth.directory import directory
from server.settings import settings


class Split(NamedTuple):
    """What a search addressed to one namespace reads, and what the cap cut."""

    read: list[str]
    skipped: list[str]


@dataclass(frozen=True)
class Fanout:
    """The caller's namespaces an all_mine search may read in `org`, before the cap."""

    org: str
    namespaces: tuple[str, ...]
    cap: int
    # The order is a priority (a scoped token's), read as strict tiers.
    ordered: bool = False
    # The public ones, which the cap drops before any private one, unless the
    # order is a priority. Not fetched when every namespace fits under the cap.
    public: frozenset[str] = frozenset()
    # The namespace kinds it spans when a search named them (knowledge.search).
    kinds: str | None = None

    def split(self, namespace: str) -> Split:
        """A search addressed to `namespace` reads it first, then up to `cap`
        of the others in their own order. When the cap cuts, it drops public
        namespaces before private ones."""
        others = [ns for ns in self.namespaces if ns != namespace]
        ranked = others
        if not self.ordered:
            ranked = [ns for ns in others if ns not in self.public]
            ranked += [ns for ns in others if ns in self.public]
        keep = set(ranked[: self.cap])
        read = [ns for ns in others if ns in keep]
        return Split([namespace, *read], [ns for ns in others if ns not in keep])


async def namespaces_to_search(
    org: str,
    namespace: str,
    namespace_scope: str | None,
    *,
    cap: int | None = None,
    kinds: str | None = None,
) -> Split:
    """What one search leg reads: the addressed `namespace`, and under
    `all_mine` the caller's fan-out besides: up to `cap` of them, of `kinds`
    (the memory fan-out's settings by default)."""
    if namespace_scope != "all_mine":
        return Split([namespace], [])
    return (await resolve_fanout(org, cap, kinds)).split(namespace)


async def resolve_fanout(org: str, cap: int | None = None, kinds: str | None = None) -> Fanout:
    """The caller's namespaces of the fan-out kinds, in their order, which is a
    priority only when `ordered` (a scoped token). `cap`: the most a search
    will read (the fan-out setting by default), so the public ones are fetched
    only when it would cut. `kinds`: the namespace kinds it spans (the memory
    fan-out's by default). Memoised per org, cap and kinds for the request:
    binding a principal clears the memo."""
    cap = settings.memory_search_fanout_max_namespaces if cap is None else cap
    cached = context.fanout_memo.get()
    if cached is not None and (cached.org, cached.cap, cached.kinds) == (org, cap, kinds):
        return cached
    # With auth off there is no token, but the single user's grants still say
    # which namespaces are theirs. With auth on, no token means no namespaces.
    auth = context.current_auth()
    subject = auth_off_subject() if auth is None else None
    readable = await context.resolve_my_namespaces(org, subject)
    # A scoped or delegated claim may repeat a namespace; it is read once.
    mine = list(dict.fromkeys(await directory().fanout_namespaces(org, readable, kinds)))
    ordered = auth is not None and auth.has_ns_roles()
    public: set[str] = set()
    if len(mine) > cap and not ordered:
        # Only a scoped token's order is a priority. For anyone else the cut
        # keeps the private namespaces, the ones the search reads first. The
        # cap counts the namespaces besides the addressed one, so this may
        # fetch visibility when nothing ends up cut.
        public = await directory().public_namespaces(org, mine)
    fanout = Fanout(
        org=org,
        namespaces=tuple(mine),
        cap=cap,
        ordered=ordered,
        public=frozenset(public),
        kinds=kinds,
    )
    context.fanout_memo.set(fanout)
    return fanout


def knowledge_search_reads_collections() -> bool:
    """Whether an all_mine knowledge search (and loom_search's knowledge leg) spans the
    caller's collections on this deployment: `knowledge_search_fanout_kinds` names
    `collection`, or is "" (every kind). The fan-out keeps a namespace with no kind row,
    but every collection has one (`collection.create` and the auto-registration write
    `collection`), so the setting alone decides. Parsed here, as
    `namespace_kinds.fanout_kinds` does, since this platform package imports no domain."""
    kinds = {k.strip() for k in str(settings.knowledge_search_fanout_kinds).split(",") if k.strip()}
    return not kinds or "collection" in kinds
