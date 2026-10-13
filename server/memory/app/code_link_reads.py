"""Which of a memory's code links are current, and the links served on reads: a memory's
`code` on memory.get, a search hit's `code_state`, the stale links namespace.validate
lists. The worker that makes and follows the links is `server.memory.app.code_links`."""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from typing import Any

from server.code.app import memory_links as code
from server.memory.domain import code_change
from server.memory.store import code_links as store
from server.settings import settings

log = logging.getLogger(__name__)

SERVED = ("active", "conflicted")


def code_namespace_for(org: str, namespace: str, tags: list[str]) -> str:
    """The trunk code namespace a memory's links live in: its own namespace's repository
    when it is written in a code namespace, else the one its `repo:` tag names."""
    return code.trunk_namespace(org, namespace) or code.namespace_for_tags(org, tags)


def inputs_hash(sha: str, files: list[str], cns: str, user_id: str) -> str:
    """Everything a memory's links are made from: its text, files, repository, user."""
    key = [sha, *sorted(files), cns, user_id]
    return hashlib.sha256("\0".join(key).encode()).hexdigest()


def memory_inputs(memory: dict[str, Any], cns: str) -> str:
    files = [str(f) for f in memory.get("files") or []]
    return inputs_hash(str(memory.get("sha") or ""), files, cns, str(memory.get("user_id") or ""))


def current(org: str, memory: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The links made from `memory` as it is now: one made for an older value, files,
    repository or user is about something else until relinking replaces it. A suggested
    correction is served only for the code it was judged against, and never on a memory a
    person holds, whenever the hold landed."""
    want = inputs_now(org, memory)
    held = code_change.held(memory)
    return [
        a if a.get("suggestion") and not held and _judged_here(a) else {**a, "suggestion": ""}
        for a in links
        if a["inputs_hash"] == want
    ]


def inputs_now(org: str, memory: dict[str, Any]) -> str:
    """The inputs hash a link made from `memory` as it is now carries."""
    tags = [str(t) for t in memory.get("tags") or []]
    return memory_inputs(memory, code_namespace_for(org, str(memory["namespace"]), tags))


def _judged_here(link: dict[str, Any]) -> bool:
    """The suggestion was judged against the code the link last saw, and that code is all
    it covers: a method its renamed class carried may behave differently at the same text."""
    if int(link.get("carried") or 0):
        return False
    return bool(link.get("judged_hash")) and link["judged_hash"] == link.get("checked_hash")


async def current_links(
    org: str, links: list[dict[str, Any]], *, served_only: bool = False
) -> list[dict[str, Any]]:
    """`links` (any memories') less those `current` drops, and those of a memory no longer
    stored (with `served_only`, no longer served either)."""
    by_mid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in links:
        by_mid[str(a["memory_id"])].append(a)
    heads = await store.memory_heads(org, list(by_mid))
    if served_only:
        heads = [h for h in heads if h["status"] in SERVED and h.get("tombstoned_at") is None]
    return [a for h in heads for a in current(org, h, by_mid.get(str(h["mid"]), []))]


async def stale_links(org: str, namespace: str) -> list[dict[str, Any]]:
    """The namespace's changed, unresolved and missing links into repositories the caller
    may read, of its served memories as they are now."""
    rows = await store.for_code(
        org, None, namespaces=[namespace], states=["changed", "unresolved", "missing"]
    )
    links = await current_links(org, rows, served_only=True)
    seen = await readable_code(org, {str(a["code_namespace"]) for a in links})
    return [a for a in links if a["code_namespace"] in seen]


async def readable_code(org: str, namespaces: set[str]) -> set[str]:
    """The code namespaces among `namespaces` the caller may read: a link names paths
    and symbols of its repository."""
    from server.auth.authz import row_in_scope
    from server.operations.base import Role

    return {ns for ns in namespaces if await row_in_scope(org, ns, Role.VIEWER)}


# ---- reads ----------------------------------------------------------------------------


async def links_of(org: str, memory_ids: list[str]) -> dict[str, list[dict[str, str]]]:
    """memory_id -> the code it is linked to, in repositories the caller may read, for a
    read's `code` field; {} when the links cannot be read."""
    try:
        rows = await current_links(org, await store.for_memories(org, memory_ids))
        seen = await readable_code(org, {str(a["code_namespace"]) for a in rows})
    except Exception as e:  # noqa: BLE001 — the links are additive to the read
        log.debug("code links unavailable: %s", e)
        return {}
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    for a in sorted(rows, key=lambda a: (str(a["path"]), str(a["qualified_name"]))):
        if a["state"] != "pending" and a["code_namespace"] in seen:
            out[str(a["memory_id"])].append(
                {
                    "code_namespace": str(a["code_namespace"]),
                    "path": str(a["path"]),
                    "symbol": str(a["qualified_name"]),
                    "state": str(a["state"]),
                    "changed_since": str(a.get("changed_since") or ""),
                    "suggestion": str(a.get("suggestion") or ""),
                }
            )
    return dict(out)


def _bound(input: Any) -> dict[str, Any]:
    """The identity a search read is bound to, as its fact leg applies it."""
    private = getattr(input, "scope", "private") == "private"
    return {
        "agent": input.agent if private and input.agent != "*" else None,
        "user_id": getattr(input, "user_id", None),
        "session_id": getattr(input, "session_id", None),
        "session_ids": set(getattr(input, "session_ids", None) or []),
    }


def _may_be(head: dict[str, Any], hit: Any, bound: dict[str, Any]) -> bool:
    """`head` is a memory the hit may have been served from: it agrees with everything
    the hit says about its row and with the read's identity bound."""
    said = {
        "agent": getattr(hit, "_row_agent", None) or bound["agent"],
        "user_id": getattr(hit, "user_id", "") or bound["user_id"],
        "session_id": getattr(hit, "session_id", "") or bound["session_id"],
    }
    if any(v and str(head[c]) != v for c, v in said.items()):
        return False
    return not bound["session_ids"] or str(head["session_id"]) in bound["session_ids"]


async def hit_memories(input: Any, hits: list[Any]) -> list[dict[str, Any] | None]:
    """Each search hit's stored memory head, with its live links as `links`: the one
    served memory under its key the hit may be; None when none or several are, or when
    its key holds no links (no memory head is read for those)."""
    if not code.code_namespaces() or getattr(input, "scope", "private") != "private" or not hits:
        return [None] * len(hits)
    keys = [(str(getattr(h, "namespace", "") or input.namespace), str(h.memory_key)) for h in hits]
    linked = await store.linked_keys(input.org, keys)
    if not linked:
        return [None] * len(hits)
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in await store.served_heads(input.org, sorted(linked)):
        by_key[(str(r["namespace"]), str(r["memory_key"]))].append(r)
    bound = _bound(input)
    out: list[dict[str, Any] | None] = []
    for h, key in zip(hits, keys, strict=True):
        fits = [r for r in by_key.get(key, []) if _may_be(r, h, bound)]
        out.append(fits[0] if len(fits) == 1 else None)
    links: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in await store.for_memories(input.org, [m["mid"] for m in out if m]):
        links[str(a["memory_id"])].append(a)
    return [
        {**m, "links": current(input.org, m, links.get(m["mid"], []))} if m else None for m in out
    ]


async def annotate_hits(input: Any, hits: list[Any]) -> None:
    """Set each search hit's `code_state` to its worst link in a repository the caller
    may read (the links themselves on `_code_links`), and drop, in place, the hits
    about code that is gone (`code_change.hidden`)."""
    try:
        memories = await hit_memories(input, hits)
        cns = {str(a["code_namespace"]) for m in memories if m for a in m["links"]}
        seen = await readable_code(input.org, cns)
        nss = {str(m["namespace"]) for m in memories if m and m["links"]}
        policies = await store.code_change_policies(input.org, sorted(nss))
    except Exception as e:  # noqa: BLE001 — the links are additive to the read
        log.debug("code links unavailable: %s", e)
        return
    kept = []
    for h, m in zip(hits, memories, strict=True):
        links = (m or {}).get("links", [])
        # a link into code the caller may not read counts as live: whether a hit goes
        # must not tell them that code is gone
        judged = [a if a["code_namespace"] in seen else a | {"state": "fresh"} for a in links]
        if m and _hidden(m, judged, policies):
            continue  # a pending link counts: its code may yet exist
        readable = [
            a for a in links if a["state"] in code_change.STATE_RANK and a["code_namespace"] in seen
        ]
        h._code_links = readable
        states = [str(a["state"]) for a in readable]
        h.code_state = max(states, key=code_change.STATE_RANK.__getitem__) if states else ""
        kept.append(h)
    hits[:] = kept


def _hidden(memory: dict[str, Any], links: list[dict[str, Any]], policies: dict[str, str]) -> bool:
    if not settings.memory_code_links_enabled:
        return False  # links that are not kept current never hide a memory
    policy = code_change.policy_of(policies.get(str(memory["namespace"]), ""))
    complete = all(a["complete"] for a in links)
    states = [str(a["state"]) for a in links]
    return code_change.hidden(states, complete, policy, code_change.held(memory))
