"""memory.for_file — memories that name one file, rendered for a pre-edit hook."""

from __future__ import annotations

import asyncio
import json
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
from server.memory.domain.code_change import STATE_RANK, worst
from server.memory.domain.decision_schema import (
    FAILURE_TAG,
    SCHEMA_NAME,
    SOURCE_CHANGED_TAG,
    is_decision,
)
from server.memory.domain.memory_types import KEY_IDENTIFIED_TYPES
from server.memory.domain.trust import confirmed as is_confirmed
from server.memory.domain.trust import row_verification

log = logging.getLogger(__name__)

_VALUE_BYTES = 600
_LINE_CHARS = 220
# The length of a `\uXXXX` escape in the stored JSON: a cut leaves at most five of one.
_ESCAPE_CHARS = 6
_CODE_NOTES = {
    "changed": " (its code changed since it was written: check before relying on it)",
    "unresolved": " (its code was renamed or removed and could not be followed: check it)",
    "missing": " (the code it is about is gone: check before relying on it)",
}
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
    substring(value, 1, {_VALUE_BYTES}) AS value,
    tags, confidence, pinned, CAST(status AS String) AS status_text, agent, updated_at, actor,
    memory_id, conflict_with, source_resource_id,
    verified_by, verified_sha256, content_sha256,
    value_len > {_VALUE_BYTES} AS value_truncated,
    arrayFilter(f -> {_FILE_MATCH}, files) AS matched_files
FROM loom.memory FINAL
WHERE org = {{org:String}} AND namespace = {{ns:String}}
  AND tombstoned_at IS NULL
  AND (expires_at IS NULL OR expires_at > now())
  -- a high row is reachable by its key only, never pushed into an edit prompt
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


def _readable(hit: dict[str, Any]) -> str:
    """The value as text: the stored JSON decoded (a string as written, anything else on one
    line), and a cut string value from its opening quote, its escapes undone up to the cut."""
    raw = str(hit.get("value") or "")
    if not hit.get("value_truncated"):
        try:
            value = json.loads(raw)
        except ValueError:
            return raw  # memory.set always stores JSON; any other text prints as stored
        return _encodable(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
    if not raw.startswith('"'):
        return raw
    # An escape the cut split (`\u00e9` cut after `\u0`) leaves the text undecodable.
    for end in range(len(raw), max(len(raw) - _ESCAPE_CHARS, 1), -1):
        try:
            text: str = json.loads(raw[:end] + '"')
        except ValueError:
            continue
        # A cut between the two escapes of a surrogate pair leaves half of one.
        text = text[:-1] if text and "\ud800" <= text[-1] <= "\udbff" else text
        # Escaped text decodes to far fewer characters than the line holds: mark the cut.
        return _encodable(text.rstrip()) + "…"
    return raw


def _encodable(text: str) -> str:
    """`text` with any lone surrogate (JSON can hold one; UTF-8 cannot) as `?`."""
    return text.encode("utf-8", "replace").decode("utf-8")


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


def _is_decision(hit: dict[str, Any]) -> bool:
    return is_decision(str(hit.get("memory_type") or ""), hit.get("tags") or [])


def _body(hit: dict[str, Any], *, decision: bool) -> str:
    """The value on one capped line; a decision's source follows it whole, so the value's
    cut never reaches it and the citation still resolves through knowledge.get."""
    text = _one_line(_readable(hit), _LINE_CHARS)
    # Only a line break would break the one-line-per-row layout; spaces stay as cited.
    source = " ".join(str(hit.get("source_resource_id") or "").splitlines())
    if decision and source:
        text += f" — source: {source}"
    return text


def _status_notes(hit: dict[str, Any], *, decision: bool) -> str:
    """`(pinned)` for a pinned row, `(unconfirmed)` for a decision not pinned or attested
    (a stale attestation does not count), `(source changed since confirmation)` or
    `(source changed)` for a decision tagged `source-changed` that is or is not confirmed,
    `(outcome: failed)` for a decision whose recorded outcome failed, and a note when its
    linked code changed or went."""
    tags = hit.get("tags") or []
    confirmed = is_confirmed(bool(hit.get("pinned")), str(hit.get("verification") or ""))
    out = ""
    if hit.get("pinned"):
        out = " (pinned)"
    elif decision and not confirmed:
        out = " (unconfirmed)"
    if decision and SOURCE_CHANGED_TAG in tags:
        out += " (source changed since confirmation)" if confirmed else " (source changed)"
    if decision and FAILURE_TAG in tags:
        out += " (outcome: failed)"
    return out + _CODE_NOTES.get(str(hit.get("code_state") or ""), "")


def _marker(hit: dict[str, Any], *, show_decision: bool) -> str:
    """` [constraint, decision]`: the row's kinds. `decision` is added only when
    `show_decision`; a team line leaves it out because its head says `Decision`."""
    kinds = [t for t in (hit.get("tags") or []) if t in ("constraint", "gotcha")]
    if show_decision:
        kinds.append("decision")
    return f" [{', '.join(kinds)}]" if kinds else ""


def _team_line(hit: dict[str, Any], now: datetime, *, decision: bool) -> str:
    """`- Team rule (by alice, 3 days ago) [constraint]: … [id: key]`
    — who wrote it and when, so a wrong rule is attributable and removable;
    a newer rule that contradicts an older one says which it supersedes.
    A decision row is headed `Decision (…)`."""
    parts = [f"by {_author(hit)}" if _author(hit) else "", _age(hit.get("updated_at"), now)]
    who = ", ".join(p for p in parts if p)
    if hit.get("supersedes_key"):
        author = hit.get("supersedes") or "another member"
        sup = f"supersedes {hit.get('supersedes_kind')} by {author}"
        who = f"{who}; {sup}" if who else sup
    label = "Decision" if decision else "Team rule"
    head = f"{label} ({who})" if who else label
    marks = _marker(hit, show_decision=False) + _status_notes(hit, decision=decision)
    return f"- {head}{marks}: {_body(hit, decision=decision)} [id: {hit['memory_key']}]"


def _removal_footer(hits: list[dict[str, Any]], home: str) -> str:
    """One line, only when team rows are present: how to remove a wrong one."""
    team = [h for h in hits if _is_team(h, home)]
    if not team:
        return ""
    ns = str(team[0].get("namespace") or "")
    # A newer rule supersedes a rule or a tagged decision; decision.record corrects a typed
    # decision, and only a maintainer may call it here.
    typed = [h.get("memory_type") == SCHEMA_NAME for h in team]
    all_typed, any_typed = all(typed), any(typed)
    steps = []
    if not all_typed:
        steps.append("write a corrected rule for the file and tell its author")
    if any_typed:
        steps.append("ask a maintainer to record the corrected decision with decision.record")
    if all_typed:
        what = "decision"
    else:
        what = "rule or decision" if any(_is_decision(h) for h in team) else "rule"
    return (
        f'- A team {what} wrong or outdated? memory.delete(namespace="{ns}", memory_key=<id>) '
        f"removes it if you wrote it or are a maintainer; otherwise {'; or '.join(steps)}."
    )


def _more_line(path: str, rest: int) -> str:
    return f'- … {rest} more; memory.for_file(path="{path}") lists them.'


def render_block(
    path: str,
    hits: list[dict[str, Any]],
    token_budget: int,
    *,
    home: str = "",
    now: datetime | None = None,
) -> tuple[str, int, bool]:
    """Prompt-ready block cut at `token_budget`. Returns (text, tokens, truncated).
    The header and the removal footer always print, and the "… more" line whenever a row
    is left out, so a budget smaller than those can be exceeded.
    `home` is the caller's namespace; a row from any other is a team row (rule or decision),
    rendered with its author and age, and the block ends with how to remove one."""
    if not hits:
        return "", 0, False
    now = now or naive_utc_now()
    budget_chars = token_budget * _CHARS_PER_TOKEN
    n = len(hits)
    lines = [f"Loom: {n} memor{'y' if n == 1 else 'ies'} anchored to {path} — read before editing:"]
    footer = _removal_footer(hits, home)
    used = len(lines[0]) + 1 + (len(footer) + 1 if footer else 0)
    rows = []
    for h in hits:
        decision = _is_decision(h)
        if _is_team(h, home):
            rows.append(_team_line(h, now, decision=decision))
        else:
            marks = _marker(h, show_decision=decision) + _status_notes(h, decision=decision)
            rows.append(f"- {h['memory_key']}{marks}: {_body(h, decision=decision)}")
    left_out = 0
    # A block that fits whole needs no "… more" line, so nothing is reserved for one.
    if used + sum(len(row) + 1 for row in rows) <= budget_chars:
        lines += rows
    else:
        hidden: set[str] = set()  # memory_ids of rows whose superseder was left out
        for i, (h, row) in enumerate(zip(hits, rows, strict=True)):
            # Keep room for a "… more" line counting every row that may still be left out.
            most = left_out + n - i - 1
            tail = len(_more_line(path, most)) + 1
            if str(h.get("memory_id")) in hidden or used + len(row) + 1 + tail > budget_chars:
                left_out += 1
                # Without the newer row above it, the row it supersedes would read as current.
                hidden.update(h.get("supersedes_ids") or [])
                continue
            lines.append(row)
            used += len(row) + 1
    truncated = left_out > 0
    if truncated:
        lines.append(_more_line(path, left_out))
    if footer:
        lines.append(footer)
    text = "\n".join(lines)
    return text, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN, truncated


def _stamp(r: dict[str, Any]) -> float:
    ts = r.get("updated_at")
    return ts.timestamp() if isinstance(ts, datetime) else 0.0


def _newer_key(r: dict[str, Any]) -> tuple[float, str]:
    return _stamp(r), str(r.get("memory_id"))


def _supersede_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Contradicting team rows (rules or decisions), reusing the conflict machinery: two rows
    of one namespace cross-linked by `conflict_with` are an older row and a newer one that
    contradicts it. The newer is placed directly above the older and carries `supersedes_key` /
    `supersedes` / `supersedes_kind` (the first older row's key, author and kind) and
    `supersedes_ids` (the memory_id of every older row it is linked to: another writer may
    hold a row under the same key). No new state: both
    stay live until the older's author or a maintainer removes it, or a verdict retires it. Two
    typed decisions never pair: a decision's key is its identity, so one cross-linked to
    another is a second decision, not a newer version."""
    by_id = {str(r.get("memory_id")): r for r in rows if r.get("memory_id")}
    newer_of: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        # The conflict check links rows of one type only, so a key-identified row's peers
        # are of its type too.
        if r.get("memory_type") in KEY_IDENTIFIED_TYPES:
            continue
        for cid in r.get("conflict_with") or []:
            peer = by_id.get(str(cid))
            if peer is None or peer is r or peer.get("namespace") != r.get("namespace"):
                continue
            # The id breaks a tie in time, so a pair links in one direction only.
            if _newer_key(r) > _newer_key(peer):
                new, old = r, peer
            else:
                new, old = peer, r
            ids = new.setdefault("supersedes_ids", [])
            if str(old.get("memory_id")) in ids:
                continue  # the pair again, seen from its other row
            ids.append(str(old.get("memory_id")))
            newer_of.setdefault(id(old), []).append(new)
            if not new.get("supersedes_key"):
                new["supersedes_key"] = str(old.get("memory_key") or "")
                new["supersedes"] = _author(old)
                new["supersedes_kind"] = "decision" if _is_decision(old) else "rule"
    if not newer_of:
        return rows
    placed: set[int] = set()
    out: list[dict[str, Any]] = []

    def place(r: dict[str, Any]) -> None:
        # The rows newer than `r` go first, theirs before them, so a chain reads newest
        # first; `placed` is set before the recursion so a cycle of links ends.
        if id(r) in placed:
            return
        placed.add(id(r))
        for new in newer_of.get(id(r), []):
            place(new)
        out.append(r)

    for r in rows:
        place(r)
    return out


async def _rows_for(
    input: MemoryForFileInput,
    namespace: str,
    path: str,
    base: str,
    tags: list[str],
    linked: asyncio.Task[dict[str, list[str]]],
) -> list[dict[str, Any]]:
    """One namespace's rows naming the file or linked to code still in it (`linked`:
    memory_id -> the states of its links here, read alongside the files match), each
    stamped with its namespace and the worst of those states."""
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
    ch = get_client()
    found, states_of = await asyncio.gather(ch.query_all(_FILES_SQL, params), linked)
    ids = sorted(m for m, states in states_of.items() if set(states) != {"missing"})
    # a hot file's links are bound a slice at a time; the best of the files match and
    # of each slice's own top `lim` are the namespace's
    if ids:
        found = found + await query_all_sliced(ch, _LINKED_SQL, params | {"linked": ids}, "linked")
    unique = {str(r["memory_id"]): r for r in found}.values()
    rows = sorted(unique, key=_order_key)[: int(input.top_k)]
    return [
        {
            **r,
            "namespace": namespace,
            "code_state": worst(states_of.get(str(r.get("memory_id")), [])),
        }
        for r in rows
    ]


async def _linked(input: MemoryForFileInput, path: str, nss: list[str]) -> dict[str, list[str]]:
    """memory_id -> the code states of the links of the `nss` memories to code in `path`,
    read from the trunk links of the repository the call is about: the one
    `extra_namespaces` or the `repo:` tag names. None named: no links (a path alone
    is in every repository)."""
    from server.code.app.memory_links import namespace_for_tags, trunk_namespace
    from server.memory.app.code_link_reads import current_links, readable_namespaces
    from server.memory.store import code_links

    trunks = [t for ns in input.extra_namespaces if (t := trunk_namespace(input.org, ns))]
    trunks = trunks or [t for t in [namespace_for_tags(input.org, input.tags)] if t]
    if not trunks:
        return {}
    try:
        links = await code_links.for_paths(
            input.org, trunks, [path], namespaces=nss, states=list(STATE_RANK)
        )
        links = await current_links(input.org, links)
        seen = await readable_namespaces(input.org, {a["code_namespace"] for a in links})
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
    linked = asyncio.ensure_future(_linked(input, path, [input.namespace, *extras]))
    try:
        own = await _rows_for(input, input.namespace, path, base, list(input.tags or []), linked)
        if not extras:
            return _supersede_order(own)
        got = await asyncio.gather(
            *(_rows_for(input, ns, path, base, [], linked) for ns in extras),
            return_exceptions=True,
        )
    finally:
        linked.cancel()  # a no-op once read; the own read's error leaves it running
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
    # The ids are not on the wire; render_block needs them to leave a superseded row out
    # with its superseder.
    shown: list[dict[str, Any]] = []
    for r in rows:
        hit = _MemoryForFileHit(
            memory_key=str(r["memory_key"]),
            memory_type=str(r.get("memory_type") or "semantic"),
            value=str(r.get("value") or ""),
            value_truncated=bool(r.get("value_truncated")),
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
            supersedes_kind=r.get("supersedes_kind") or "",
            matched_files=[str(f) for f in (r.get("matched_files") or [])],
            source_resource_id=str(r.get("source_resource_id") or ""),
            verification=row_verification(r),
            code_state=r.get("code_state", ""),
        )
        hits.append(hit)
        shown.append(
            hit.model_dump()
            | {"memory_id": r.get("memory_id"), "supersedes_ids": r.get("supersedes_ids") or []}
        )

    rendered, tokens, truncated = render_block(
        path, shown, input.token_budget, home=input.namespace
    )
    return MemoryForFileOutput(
        path=path,
        total=len(hits),
        hits=hits,
        rendered=rendered,
        tokens_estimate=tokens,
        truncated=truncated,
    )
