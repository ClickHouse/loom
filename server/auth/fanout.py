"""What a `namespace_scope=all_mine` search reads.

The caller's own namespaces (`resolve_my_namespaces`), narrowed to the kinds a
fan-out sweeps and cut at `memory_search_fanout_max_namespaces`, plus the
namespace the call addressed, which every leg reads whatever the list says.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from server.auth import context
from server.auth.directory import directory


@dataclass(frozen=True)
class Fanout:
    """What an all_mine search reads in `org`, and what its cap left out."""

    org: str
    namespaces: tuple[str, ...]
    skipped: tuple[str, ...]


def with_addressed(namespace: str, fanout: Sequence[str]) -> list[str]:
    """The namespaces an all_mine leg reads: `fanout`, led by the addressed
    `namespace` when the fan-out lacks it (cut at the cap, or of another
    kind). The transport authorized that namespace, and a fan-out only widens
    a search, never moves it."""
    out = [ns for ns in fanout if ns]
    return out if not namespace or namespace in out else [namespace, *out]


async def namespaces_to_search(org: str, namespace: str, namespace_scope: str | None) -> list[str]:
    """The namespaces one search leg reads: the addressed `namespace`, and
    under `all_mine` the caller's fan-out besides."""
    if namespace_scope != "all_mine":
        return [namespace]
    return with_addressed(namespace, (await resolve_fanout(org)).namespaces)


async def resolve_fanout(org: str) -> Fanout:
    """The namespaces a `namespace_scope=all_mine` search spans:
    `resolve_my_namespaces` narrowed to the kinds in
    `memory_search_fanout_kinds`, then cut at
    `memory_search_fanout_max_namespaces`. A code or QA index is a namespace
    the caller can read, not one a personal-fact search should sweep; it is
    addressed by name or by its own tools. Order (= read priority) is kept.
    Unknown kinds stay: the directory can only narrow, never widen. Resolved
    once per bound principal and org."""
    from server.auth.default_namespace import auth_off_subject
    from server.settings import settings

    memo = context.fanout_memo.get()
    if memo is not None and memo.org == org:
        return memo
    # With auth off there is no token, but the single user's grants still say
    # which namespaces are theirs. With auth on, no token means no namespaces.
    auth = context.current_auth()
    subject = auth_off_subject() if auth is None else None
    readable = await context.resolve_my_namespaces(org, subject)
    mine = await directory().fanout_namespaces(org, readable)
    cap = settings.memory_search_fanout_max_namespaces
    ranked = mine
    if len(mine) > cap and not (auth is not None and auth.has_ns_roles()):
        # Only a scoped token's order is a priority. For anyone else the cut
        # keeps the private namespaces, the ones the search reads first.
        public = await directory().public_namespaces(org, mine)
        ranked = sorted(mine, key=lambda ns: ns in public)
    keep = set(ranked[:cap])
    memo = Fanout(
        org=org,
        namespaces=tuple(ns for ns in mine if ns in keep),
        skipped=tuple(ns for ns in mine if ns not in keep),
    )
    context.fanout_memo.set(memo)
    return memo
