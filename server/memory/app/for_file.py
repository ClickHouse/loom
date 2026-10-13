"""memory.for_file — memories that name one file, rendered for a pre-edit hook."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from server.code.app.common import repo_relative
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.repo_base import query_all_sliced
from server.memory.api.models_for_file import (
    MemoryForFileInput,
    MemoryForFileOutput,
    _MemoryForFileHit,
)
from server.memory.domain.code_change import STATE_RANK

log = logging.getLogger(__name__)

_VALUE_CHARS = 600
_LINE_CHARS = 220
# Rough prompt-token estimate for English prose and code paths.
_CHARS_PER_TOKEN = 4

# A file reference matches the requested path when either side is the bare
# filename of the other, or both are the same string. `p` is the request,
# `b` its basename, `f` a stored files[] entry.
_FILE_MATCH = (
    "(f = {p:String} OR f = {b:String} "
    "OR endsWith(f, concat('/', {b:String})) "
    "OR endsWith({p:String}, concat('/', f)))"
)

_SELECT = f"""
SELECT
    memory_key, memory_type,
    substring(value, 1, {_VALUE_CHARS}) AS value,
    tags, confidence, pinned, CAST(status AS String) AS status_text, agent, updated_at, actor,
    memory_id, conflict_with,
    arrayFilter(f -> {_FILE_MATCH}, files) AS matched_files
FROM loom.memory FINAL
WHERE org = {{org:String}} AND namespace = {{ns:String}}
  AND tombstoned_at IS NULL
  AND (expires_at IS NULL OR expires_at > now())
  -- a credential is reachable by its key only, never pushed into an edit prompt
  AND sensitivity != 'high'
  AND status IN {{statuses:Array(String)}}
  AND (length({{tags:Array(String)}}) = 0 OR hasAny(tags, {{tags:Array(String)}}))
"""
_ORDER = """
ORDER BY has(tags, 'constraint') DESC, pinned DESC, confidence DESC, updated_at DESC
LIMIT {lim:UInt32}
"""
_FILES_SQL = f"{_SELECT}  AND arrayExists(f -> {_FILE_MATCH}, files){_ORDER}"
_LINKED_SQL = f"{_SELECT}  AND memory_id IN {{linked:Array(UUID)}}{_ORDER}"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _author(hit: dict[str, Any]) -> str:
    """The display form of the server-stamped actor: the
    subject, without the `human:` / `process:` kind."""
    actor = str(hit.get("actor") or "")
    for prefix in ("human:", "process:"):
        if actor.startswith(prefix):
            return actor[len(prefix) :]
    return actor


def _age(ts: Any, now: datetime) -> str:
    """'3 days ago' — a relative age for a naive-UTC or aware timestamp."""
    if not isinstance(ts, datetime):
        return ""
    ref = now if ts.tzinfo is None else now.replace(tzinfo=UTC)
    secs = max(0, int((ref - ts).total_seconds()))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def _is_team(hit: dict[str, Any], home: str) -> bool:
    ns = str(hit.get("namespace") or "")
    return bool(home and ns and ns != home)


def _team_line(hit: dict[str, Any], marker: str, pin: str, now: datetime) -> str:
    """`- Team rule (by alice, 3 days ago) [constraint]: … [id: key]`
    — who wrote it and when, so a wrong rule is attributable and removable;
    a newer rule that contradicts an older one says which it supersedes."""
    parts = [f"by {_author(hit)}" if _author(hit) else "", _age(hit.get("updated_at"), now)]
    who = ", ".join(p for p in parts if p)
    if hit.get("supersedes_key"):
        sup = f"supersedes rule by {hit.get('supersedes') or 'another member'}"
        who = f"{who}; {sup}" if who else sup
    head = f"Team rule ({who})" if who else "Team rule"
    return (
        f"- {head}{marker}{pin}: {_one_line(hit.get('value') or '', _LINE_CHARS)} "
        f"[id: {hit['memory_key']}]"
    )


def _removal_footer(hits: list[dict[str, Any]], home: str) -> str:
    """One line, only when team rules are present: how to remove a wrong one."""
    team = [h for h in hits if _is_team(h, home)]
    if not team:
        return ""
    ns = str(team[0].get("namespace") or "")
    return (
        f'- A team rule wrong or outdated? memory.delete(namespace="{ns}", memory_key=<id>) '
        "removes it if you wrote it or are an admin; otherwise write a corrected rule "
        "for the file and tell its author."
    )


def render_block(
    path: str,
    hits: list[dict[str, Any]],
    token_budget: int,
    *,
    home: str = "",
    now: datetime | None = None,
) -> tuple[str, int, bool]:
    """Prompt-ready block cut at `token_budget`. Returns (text, tokens, truncated).
    `home` is the caller's namespace; a row from any other is a team rule,
    rendered with its author and age, and the block ends with how to remove one."""
    if not hits:
        return "", 0, False
    now = now or naive_utc_now()
    budget_chars = token_budget * _CHARS_PER_TOKEN
    n = len(hits)
    lines = [f"Loom: {n} memor{'y' if n == 1 else 'ies'} anchored to {path} — read before editing:"]
    footer = _removal_footer(hits, home)
    used = len(lines[0]) + 1 + (len(footer) + 1 if footer else 0)
    truncated = False
    for h in hits:
        tags = [t for t in (h.get("tags") or []) if t in ("constraint", "decision", "gotcha")]
        marker = f" [{', '.join(tags)}]" if tags else ""
        pin = " (pinned)" if h.get("pinned") else ""
        if h.get("code_state") == "changed":
            pin += " (its code changed since it was written: check before relying on it)"
        elif h.get("code_state") == "unresolved":
            pin += " (its code was renamed or removed and could not be followed: check it)"
        elif h.get("code_state") == "missing":
            pin += " (the code it is about is gone: check before relying on it)"
        line = (
            _team_line(h, marker, pin, now)
            if _is_team(h, home)
            else f"- {h['memory_key']}{marker}{pin}: {_one_line(h.get('value') or '', _LINE_CHARS)}"
        )
        if used + len(line) + 1 > budget_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    if truncated:
        rest = n - (len(lines) - 1)
        tail = f'- … {rest} more; memory.for_file(path="{path}") lists them.'
        lines.append(tail)
        used += len(tail) + 1
    if footer:
        lines.append(footer)
    text = "\n".join(lines)
    return text, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN, truncated


def _stamp(r: dict[str, Any]) -> float:
    ts = r.get("updated_at")
    return ts.timestamp() if isinstance(ts, datetime) else 0.0


def _supersede_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Contradicting team rules, reusing the conflict machinery: two rows of one namespace
    cross-linked by `conflict_with` are an older rule and a newer one that contradicts it. The
    newer is placed directly above the older and carries `supersedes_key` / `supersedes` (the
    older's key and author). No new state: both stay live until the older's author or an admin
    removes it, or a verdict retires it."""
    by_id = {str(r.get("memory_id")): r for r in rows if r.get("memory_id")}
    newer_of: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        for cid in r.get("conflict_with") or []:
            peer = by_id.get(str(cid))
            if peer is None or peer is r or peer.get("namespace") != r.get("namespace"):
                continue
            new, old = (r, peer) if _stamp(r) >= _stamp(peer) else (peer, r)
            if not new.get("supersedes_key"):
                new["supersedes_key"] = str(old.get("memory_key") or "")
                new["supersedes"] = _author(old)
                newer_of.setdefault(id(old), []).append(new)
    if not newer_of:
        return rows
    placed: set[int] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        for new in newer_of.get(id(r), []):
            if id(new) not in placed:
                placed.add(id(new))
                out.append(new)
        if id(r) not in placed:
            placed.add(id(r))
            out.append(r)
    return out


async def _rows_for(
    input: MemoryForFileInput,
    namespace: str,
    path: str,
    base: str,
    tags: list[str],
    linked: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """One namespace's rows naming the file or linked to code still in it (`linked`:
    memory_id -> the states of its links here), each stamped with its namespace and the
    worst of those states."""
    statuses = ["active", "conflicted"] if input.include_conflicted else ["active"]
    params = {
        "org": input.org,
        "ns": namespace,
        "p": path,
        "b": base,
        "tags": tags,
        "statuses": statuses,
        "lim": int(input.top_k),
    }
    ids = sorted(m for m, states in linked.items() if set(states) != {"missing"})
    ch = get_client()
    found = await ch.query_all(_FILES_SQL, params)
    # a hot file's links are bound a slice at a time; the best of the files match and
    # of each slice's own top `lim` are the namespace's
    if ids:
        found = found + await query_all_sliced(ch, _LINKED_SQL, params | {"linked": ids}, "linked")
    unique = {str(r["memory_id"]): r for r in found}.values()
    rows = sorted(unique, key=_order_key)[: int(input.top_k)]
    return [
        {**r, "namespace": namespace, "code_state": _worst(linked.get(str(r.get("memory_id")), []))}
        for r in rows
    ]


def _worst(states: list[str]) -> str:
    return max(states, key=STATE_RANK.__getitem__, default="")


async def _linked(input: MemoryForFileInput, path: str, nss: list[str]) -> dict[str, list[str]]:
    """memory_id -> the code states of the links of the `nss` memories to code in `path`,
    read from the trunk links of the repository the call is about: the one
    `extra_namespaces` or the `repo:` tag names. None named: no links (a path alone
    is in every repository)."""
    from server.code.app.memory_links import namespace_for_tags, trunk_namespace
    from server.memory.app.code_link_reads import current_links, readable_code
    from server.memory.store import code_links

    trunks = [t for ns in input.extra_namespaces if (t := trunk_namespace(input.org, ns))]
    trunks = trunks or [t for t in [namespace_for_tags(input.org, input.tags)] if t]
    if not trunks:
        return {}
    try:
        links = await code_links.for_code(
            input.org, trunks, paths=[path], namespaces=nss, states=list(STATE_RANK)
        )
        links = await current_links(input.org, links)
        seen = await readable_code(input.org, {a["code_namespace"] for a in links})
    except Exception as e:  # noqa: BLE001 — the links are additive; the files match stands
        log.debug("memory.for_file: code links skipped: %s", e)
        return {}
    out: dict[str, list[str]] = defaultdict(list)
    for a in links:
        if a["code_namespace"] in seen:
            out[a["memory_id"]].append(a["state"])
    return out


async def _readable_extras(input: MemoryForFileInput) -> list[str]:
    """The extra namespaces the caller may read, deduplicated,
    the request's own namespace dropped; an unreadable one is skipped, never
    an error (the gate is `row_in_scope`, the same check the transport makes)."""
    from server.auth.authz import row_in_scope
    from server.operations.base import Role

    out: list[str] = []
    for raw in input.extra_namespaces or []:
        ns = str(raw or "").strip()
        if not ns or ns == input.namespace or ns in out:
            continue
        if await row_in_scope(input.org, ns, Role.VIEWER):
            out.append(ns)
    return out


def _order_key(r: dict[str, Any]) -> tuple[Any, ...]:
    """The SQL's ORDER BY, applied across namespaces: constraint, pinned,
    confidence, then newest."""
    conf = r.get("confidence")
    return (
        "constraint" not in (r.get("tags") or []),
        not r.get("pinned"),
        -(float(conf) if conf is not None else 1.0),
        -_stamp(r),
    )


async def _merged_rows(input: MemoryForFileInput, path: str, base: str) -> list[dict[str, Any]]:
    """The caller's namespace, plus each readable extra namespace (tag filter on the caller's
    only), merged in constraint-first order, deduplicated by (namespace, memory_key), cut at
    top_k. An extra namespace's failed read is dropped; the caller's own raises."""
    extras = await _readable_extras(input)
    linked = await _linked(input, path, [input.namespace, *extras])
    own = await _rows_for(input, input.namespace, path, base, list(input.tags or []), linked)
    if not extras:
        return _supersede_order(own)
    got = await asyncio.gather(
        *(_rows_for(input, ns, path, base, [], linked) for ns in extras), return_exceptions=True
    )
    rows = list(own)
    for ns, res in zip(extras, got, strict=True):
        if isinstance(res, BaseException):
            log.debug("memory.for_file: extra namespace %s skipped: %s", ns, res)
            continue
        rows.extend(res)
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for r in sorted(rows, key=_order_key):
        k = (str(r["namespace"]), str(r["memory_key"]))
        if k not in seen:
            seen.add(k)
            merged.append(r)
    return _supersede_order(merged[: int(input.top_k)])


async def handle_memory_for_file(input: MemoryForFileInput) -> MemoryForFileOutput:
    path = repo_relative(input.path)
    base = path.rsplit("/", 1)[-1]
    rows = await _merged_rows(input, path, base)

    hits: list[_MemoryForFileHit] = []
    for r in rows or []:
        hits.append(
            _MemoryForFileHit(
                memory_key=str(r["memory_key"]),
                memory_type=str(r.get("memory_type") or "semantic"),
                value=str(r.get("value") or ""),
                tags=[str(t) for t in (r.get("tags") or [])],
                confidence=float(conf) if (conf := r.get("confidence")) is not None else 1.0,
                pinned=bool(r.get("pinned")),
                status=str(r.get("status_text") or "active"),
                agent=str(r.get("agent") or ""),
                updated_at=r.get("updated_at"),
                namespace=str(r.get("namespace") or input.namespace),
                actor=str(r.get("actor") or ""),
                supersedes_key=str(r.get("supersedes_key") or ""),
                supersedes=str(r.get("supersedes") or ""),
                matched_files=[str(f) for f in (r.get("matched_files") or [])],
                code_state=str(r.get("code_state") or ""),
            )
        )

    rendered, tokens, truncated = render_block(
        path, [h.model_dump() for h in hits], input.token_budget, home=input.namespace
    )
    return MemoryForFileOutput(
        path=path,
        total=len(hits),
        hits=hits,
        rendered=rendered,
        tokens_estimate=tokens,
        truncated=truncated,
    )
