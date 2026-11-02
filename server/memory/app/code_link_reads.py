"""Which of a memory's code links are current, and the links served on reads: a memory's
`code` on memory.get, a search hit's `code_state`, the stale links namespace.validate
lists. The worker that makes and follows the links is `server.memory.app.code_links`."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from server.code.app import memory_links as code
from server.code.domain.index.remote import repo_tag
from server.code.domain.index.repos import configured_repos
from server.memory.api.models_crud import CodeLink
from server.memory.domain import code_change
from server.memory.store import code_links as store
from server.memory.store.code_links import LinkRow
from server.settings import settings

log = logging.getLogger(__name__)

SERVED = ("active", "conflicted")


def code_namespace_for(org: str, namespace: str, tags: list[str]) -> str:
    """The trunk code namespace a memory's links live in: its own namespace's repository
    when it is written in a code namespace, else the one its `repo:` tag names."""
    return code.trunk_namespace(org, namespace) or code.namespace_for_tags(org, tags)


def inputs_hash(sha: str, files: list[str], cns: str, user_id: str) -> str:
    """Everything a memory's links are made from: its text, files, repository, user
    (`store.inputs_hash_sql` reads the same in SQL)."""
    key = [sha, *sorted(files), cns, user_id]
    return hashlib.sha256("\0".join(key).encode()).hexdigest()


def memory_inputs(memory: dict[str, Any], cns: str) -> str:
    files = [str(f) for f in memory.get("files") or []]
    return inputs_hash(str(memory.get("sha") or ""), files, cns, str(memory.get("user_id") or ""))


def current(org: str, memory: dict[str, Any], links: list[LinkRow]) -> list[LinkRow]:
    """The links made from `memory` as it is now: one made for an older value, files,
    repository or user is about something else until relinking replaces it. A suggested
    correction is served only for the code it was judged against, and never on a memory a
    person holds, whenever the hold landed."""
    want = inputs_now(org, memory)
    held = code_change.held(memory)
    return [
        a if a["suggestion"] and not held and _judged_here(a) else {**a, "suggestion": ""}
        for a in links
        if a["inputs_hash"] == want
    ]


def inputs_now(org: str, memory: dict[str, Any]) -> str:
    """The inputs hash a link made from `memory` as it is now carries."""
    tags = [str(t) for t in memory.get("tags") or []]
    return memory_inputs(memory, code_namespace_for(org, str(memory["namespace"]), tags))


def _judged_here(link: LinkRow) -> bool:
    """The suggestion was judged against the code the link last saw, and that code is all
    it covers: a method its renamed class carried may behave differently at the same text."""
    if link["carried"]:
        return False
    return bool(link["judged_hash"]) and link["judged_hash"] == link["checked_hash"]


async def current_links(org: str, links: list[LinkRow]) -> list[LinkRow]:
    """The links among `links` that `current` keeps for their stored memory."""
    by_mid: dict[str, list[LinkRow]] = defaultdict(list)
    for a in links:
        by_mid[a["memory_id"]].append(a)
    heads = await store.memory_heads(org, list(by_mid))
    return [a for h in heads for a in current(org, h, by_mid.get(str(h["mid"]), []))]


async def stale_links(
    org: str, namespace: str, keys: Sequence[str] | None = None, *, limit: int
) -> list[dict[str, Any]]:
    """Up to `limit` of the namespace's changed, unresolved and missing links into
    repositories the caller may read, of its served memories (those under `keys` when
    given) as they are now, one per memory, newest check first; each with its memory_key,
    memory_id, code_namespace, path, qualified_name, state, changed_since, checked_at and
    `total`, the number of memories they are of."""
    trunk = code.trunk_namespace(org, namespace)
    tagged = [] if trunk else _tag_namespaces(org)
    seen = await readable_namespaces(org, {trunk, *(cns for _, cns in tagged)} - {""})
    if not seen:
        return []
    return await store.stale_of_served(
        org,
        namespace,
        ["changed", "unresolved", "missing"],
        memory_keys=keys,
        trunk=trunk,
        tag_namespaces=tagged,
        readable=sorted(seen),
        limit=limit,
    )


def _tag_namespaces(org: str) -> list[tuple[str, str]]:
    """(repo tag, trunk namespace) of the org's repositories, in the order
    `namespace_for_tags` tries them."""
    return [
        (repo_tag(cfg.git_remote or cfg.repo), code.trunk_namespace(org, cfg.namespace))
        for cfg in configured_repos()
        if cfg.org == org
    ]


async def readable_namespaces(org: str, namespaces: set[str]) -> set[str]:
    """The namespaces among `namespaces` the caller may read."""
    from server.auth.authz import row_in_scope
    from server.operations.base import Role

    names = sorted(namespaces)
    allowed = await asyncio.gather(
        *(row_in_scope(org, ns, Role.VIEWER, hides_answer=False) for ns in names)
    )
    return {ns for ns, ok in zip(names, allowed, strict=True) if ok}


# ---- reads ----------------------------------------------------------------------------


def head_of(row: Any) -> dict[str, Any]:
    """A read's memory row in the head shape `current` takes."""

    def get(name: str, default: Any = "") -> Any:
        return getattr(row, name, None) or default

    return {
        "mid": str(row.memory_id),
        "namespace": get("namespace"),
        "user_id": get("user_id"),
        "tags": list(get("tags", [])),
        "files": list(get("files", [])),
        "sha": get("content_sha256"),
        "pinned": get("pinned", False),
        "lock_mode": get("lock_mode"),
        "lock_until": get("lock_until", None),
        "verified_by": get("verified_by"),
        "verified_sha256": get("verified_sha256"),
    }


async def links_of(org: str, memory: dict[str, Any]) -> list[CodeLink]:
    """The code `memory` (a head) is linked to, in repositories the caller may read, for
    a read's `code` field; [] when the links cannot be read."""
    if not code.code_namespaces():
        return []
    tags = [str(t) for t in memory.get("tags") or []]
    if not code_namespace_for(org, str(memory["namespace"]), tags):
        return []  # no link is made from a memory that names no repository
    try:
        rows = current(org, memory, await store.for_memories(org, [str(memory["mid"])]))
        seen = await readable_namespaces(org, {a["code_namespace"] for a in rows})
    except Exception as e:  # noqa: BLE001 — the links are additive to the read
        log.debug("code links unavailable: %s", e)
        return []
    return [
        CodeLink.model_validate(
            {
                "code_namespace": a["code_namespace"],
                "path": a["path"],
                "symbol": a["qualified_name"],
                "state": a["state"],
                "changed_since": a["changed_since"],
                "suggestion": a["suggestion"],
            }
        )
        for a in sorted(rows, key=lambda a: (a["path"], a["qualified_name"]))
        if a["state"] != "pending" and a["code_namespace"] in seen
    ]


_IDENT = ("namespace", "agent", "user_id", "session_id", "memory_key")
Ident = tuple[str, str, str, str, str]


def _ident_of(row: Mapping[str, Any]) -> Ident:
    return cast(Ident, tuple(str(row[c]) for c in _IDENT))


def _identity(input: Any, hit: Any) -> Ident | None:
    """(namespace, agent, user_id, session_id, memory_key) of the memory the hit was read
    from; None when its leg did not read who wrote it."""
    who = getattr(hit, "_row_identity", None)
    if who is None:
        return None
    return (str(getattr(hit, "namespace", "") or input.namespace), *who, str(hit.memory_key))


async def _hit_links(
    input: Any, hits: list[Any]
) -> tuple[list[Ident | None], dict[Ident, list[LinkRow]]]:
    """Each hit's memory identity, and the live links of those memories."""
    if not code.code_namespaces() or not hits:
        return [None] * len(hits), {}
    idents = [_identity(input, h) for h in hits]
    links: dict[Ident, list[LinkRow]] = defaultdict(list)
    for a in await store.live_links(input.org, sorted({i for i in idents if i})):
        links[_ident_of(a)].append(a)
    return idents, links


def _heads_of(
    org: str,
    idents: list[Ident | None],
    links: dict[Ident, list[LinkRow]],
    heads: list[dict[str, Any]],
) -> list[dict[str, Any] | None]:
    """Each hit's served memory head with its current links as `links`; None for a hit
    whose memory holds no link."""
    out: dict[Ident, dict[str, Any]] = {}
    for h in heads:
        ident = _ident_of(h)
        mine = [a for a in links.get(ident, []) if a["memory_id"] == str(h["mid"])]
        if mine:
            out[ident] = {**h, "links": current(org, h, mine)}
    return [out.get(i) if i else None for i in idents]


def _linked_keys(links: dict[Ident, list[LinkRow]]) -> list[tuple[str, str]]:
    return sorted({(i[0], i[4]) for i in links})


async def hit_memories(input: Any, hits: list[Any]) -> list[dict[str, Any] | None]:
    """Each search hit's stored memory head, with its live links as `links`: the memory
    the hit was read from; None when that is not known, or when it holds no links (no
    memory head is read for those)."""
    idents, links = await _hit_links(input, hits)
    heads = await store.served_heads(input.org, _linked_keys(links)) if links else []
    return _heads_of(input.org, idents, links, heads)


async def _annotation_reads(
    input: Any, hits: list[Any]
) -> tuple[list[dict[str, Any] | None], set[str], dict[str, str]]:
    """`hit_memories`, the code namespaces the caller may read and the code-change
    policies: the last two read beside the memory heads, and only once a hit holds a link."""
    idents, links = await _hit_links(input, hits)
    if not links:
        return [None] * len(hits), set(), {}
    heads, seen, policies = await asyncio.gather(
        store.served_heads(input.org, _linked_keys(links)),
        readable_namespaces(input.org, {a["code_namespace"] for v in links.values() for a in v}),
        store.code_change_policies(input.org, sorted({i[0] for i in links})),
    )
    return _heads_of(input.org, idents, links, heads), seen, policies


async def annotate_hits(input: Any, hits: list[Any]) -> None:
    """Set each search hit's `code_state` to its worst link in a repository the caller
    may read (the links themselves on `_code_links`), and drop, in place, the hits
    about code that is gone (`code_change.hidden`)."""
    try:
        memories, seen, policies = await _annotation_reads(input, hits)
    except Exception as e:  # noqa: BLE001 — the links are additive to the read
        log.debug("code links unavailable: %s", e)
        return
    kept = []
    for h, m in zip(hits, memories, strict=True):
        links: list[LinkRow] = (m or {}).get("links", [])
        # a link into code the caller may not read counts as live: whether a hit goes
        # must not tell them that code is gone
        judged = [a if a["code_namespace"] in seen else a | {"state": "fresh"} for a in links]
        # a pending link keeps the memory: its code may yet exist
        if m and _hidden(m, judged, policies):
            continue
        shown = [
            a for a in links if a["state"] in code_change.STATE_RANK and a["code_namespace"] in seen
        ]
        h._code_links = shown
        h.code_state = code_change.worst([a["state"] for a in shown])
        kept.append(h)
    hits[:] = kept


def _hidden(memory: dict[str, Any], links: list[LinkRow], policies: dict[str, str]) -> bool:
    if not settings.memory_code_links_enabled:
        return False  # links that are not kept current never hide a memory
    policy = code_change.policy_of(policies.get(str(memory["namespace"]), ""))
    complete = all(a["complete"] for a in links)
    states = [a["state"] for a in links]
    return code_change.hidden(states, complete, policy, code_change.held(memory))
