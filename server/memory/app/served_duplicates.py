"""Duplicates are not served.

Set-time dedup at 0.97 cosine inserts a near-copy as `status='duplicate'`
with `duplicate_of` naming the canonical, and tells the writer so. If reads
ignored that status, duplicates would be returned by memory.search and
memory.list and recalled as often as the canonical.

This module is the read-side rule: a search window carries no `duplicate`
row unless the call asks (`include_duplicates`); the duplicate's canonical
takes its slot when it is active and not already in the pool, disclosed as
`resolved_from`; memory.list skips them; memory.get keeps returning the
exact row and names the canonical. The status of a row is read from
`loom.memory`, not from the search leg's mirror: older duplicates carry
`active` in `memory_embeddings`, so the leg's own predicate — tightened here as
well — cannot see them. Both lookups stay inside the request's read scope: a
canonical another user (or session) owns is treated as gone, never named.
"""

from __future__ import annotations

import logging
from typing import Any

from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import ReadFilters
from server.memory.domain.search_types import RowKey, hit_row_key
from server.memory.store.dedup import CanonicalRow, DuplicateStatus

log = logging.getLogger(__name__)


def hide_duplicates(input: Any) -> bool:
    """Whether this read hides `duplicate` rows: always, unless the caller
    asked for them (`include_duplicates`)."""
    return not bool(getattr(input, "include_duplicates", False))


def _is_fact_hit(h: Any) -> bool:
    return (getattr(h, "source", "fact") or "fact") == "fact" and not str(
        getattr(h, "memory_key", "") or ""
    ).startswith("chunk:")


def _hit_ns(h: Any, default_ns: str) -> str:
    return str(getattr(h, "namespace", "") or default_ns)


def _excerpt_of(value: Any, memory_key: str) -> str:
    """The canonical's excerpt, built the way its own embedding row was."""
    from server.memory.domain.excerpt import build_excerpt

    raw = value if isinstance(value, str) else str(value)
    return build_excerpt(raw, memory_key=memory_key)


def _substitute(h: Any, row: CanonicalRow, ns: str) -> Any:
    """The canonical in the duplicate's slot: its key, text and lifecycle
    fields, the duplicate's scores (the slot is the duplicate's), and
    `resolved_from` naming the row it answers for."""
    key = row.memory_key
    valid_at = row.valid_at
    update: dict[str, Any] = {
        "memory_key": key,
        "memory_type": row.memory_type or str(h.memory_type),
        "content_excerpt": _excerpt_of(row.value, key),
        "status": "active",
        "version": row.version,
        "created_at": row.created_at,
        "valid_at": "" if valid_at.startswith("1970-01-01") else valid_at,
        "session_id": row.session_id,
        "source_class": row.source_class,
        "entities": [],
        "verified": False,
        "namespace": getattr(h, "namespace", "") or "",
        "resolved_from": str(h.memory_key),
    }
    if getattr(h, "tags", None) is not None:
        update["tags"] = list(row.tags)
    out = h.model_copy(update=update)
    # the canonical entered the pool through no leg's WHERE: give it its own tags and
    # identity, not the duplicate's that `model_copy` carried over
    try:
        out._row_tags = row.tags
        out._row_identity = (row.agent, row.user_id, row.session_id)
    except Exception:  # noqa: BLE001 — a foreign hit type stays as it is
        pass
    return out


_Truth = dict[RowKey, DuplicateStatus]
_Canon = dict[tuple[str, str], CanonicalRow]


async def _truth(org: str, rows: list[RowKey], scope: ReadFilters) -> _Truth:
    """Each pooled row's status. A hit that names no row (a stored row's user_id is never
    empty) takes its key's status, as one row of the key would."""
    import server.memory.store as memory_repo

    named = [r for r in rows if r[2]]
    unnamed = [r for r in rows if not r[2]]
    truth: _Truth = {}
    if named:
        found = await memory_repo.status_by_rows(org, named, scope=scope)
        truth = {s.row_key: s for s in found}
    if unnamed:
        keys = sorted({(r[0], r[4]) for r in unnamed})
        found = await memory_repo.status_by_keys(org, keys, scope=scope)
        by_key = {(s.namespace, s.memory_key): s for s in found}
        truth.update({r: by_key[(r[0], r[4])] for r in unnamed if (r[0], r[4]) in by_key})
    return truth


async def _canonicals(
    org: str, namespaces: list[str], ids: list[str], scope: ReadFilters
) -> _Canon:
    import server.memory.store as memory_repo

    rows = await memory_repo.canonical_rows_by_ids(org, namespaces, ids, scope=scope)
    # A high canonical is readable by key only: its duplicate is hidden with no substitute.
    return {(r.namespace, r.canonical_id): r for r in rows if r.sensitivity != "high"}


def _is_duplicate(t: DuplicateStatus | None) -> bool:
    return t is not None and t.status == "duplicate"


async def hide_pool_duplicates(ctx: Any) -> None:
    """Drop the pool's duplicate rows and seat their canonicals before the
    window is cut (after `exclude_keys`), so the window stays
    `top_k` long. One read of the pooled rows; a second, over the
    canonical ids, only when a duplicate was in the pool. Fail-open: a failed
    read leaves the pool as the legs ranked it. Under `as_of` a duplicate is
    hidden but not substituted — the canonical's own validity window was not
    checked at the anchor."""
    input = ctx.input
    if not hide_duplicates(input):
        return
    facts = [h for h in ctx.all_hits if _is_fact_hit(h)]
    if not facts:
        return
    ns_default = str(input.namespace)
    scope = request_scope()
    try:
        truth = await _truth(input.org, [hit_row_key(h, ns_default) for h in facts], scope)
    except Exception as e:  # noqa: BLE001 — hiding is a filter, never a failure
        log.warning("memory.search: duplicate status read failed: %s", e)
        return
    dups = [h for h in facts if _is_duplicate(truth.get(hit_row_key(h, ns_default)))]
    if not dups:
        return
    canon: _Canon = {}
    if getattr(input, "as_of", None) is None:
        ids = [truth[hit_row_key(h, ns_default)].canonical_id for h in dups]
        try:
            canon = await _canonicals(
                input.org, sorted({_hit_ns(h, ns_default) for h in dups}), ids, scope
            )
        except Exception as e:  # noqa: BLE001 — hiding is a filter, never a failure
            log.warning("memory.search: canonical read failed: %s", e)
    ctx.all_hits = _rewrite_pool(ctx.all_hits, dups, truth, canon, ns_default, ctx.actual_work)


def _rewrite_pool(
    hits: list[Any],
    dups: list[Any],
    truth: _Truth,
    canon: _Canon,
    ns_default: str,
    work: dict[str, Any],
) -> list[Any]:
    dup_ids = {id(h) for h in dups}
    present = {hit_row_key(h, ns_default) for h in hits if id(h) not in dup_ids}
    out: list[Any] = []
    resolved = 0
    for h in hits:
        if id(h) not in dup_ids:
            out.append(h)
            continue
        ns = _hit_ns(h, ns_default)
        cid = truth[hit_row_key(h, ns_default)].canonical_id
        row = canon.get((ns, cid))
        if row is None:
            continue
        canonical = row.row_key
        if canonical in present:
            continue
        present.add(canonical)
        out.append(_substitute(h, row, ns))
        resolved += 1
    work["duplicates_hidden"] = len(dups)
    work["duplicates_resolved"] = resolved
    return out


async def canonical_keys_for(org: str, namespace: str, canonical_ids: list[str]) -> dict[str, str]:
    """`duplicate_of` id → the canonical's memory_key, for the ids whose row
    is live, active and inside the request's read scope; a missing id means
    the canonical is gone to this read (the orphan case
    `memory.resolve_orphan_duplicates` re-activates, or another user's row)."""
    ids = sorted({str(i) for i in canonical_ids if i})
    if not ids:
        return {}
    import server.memory.store as memory_repo

    rows = await memory_repo.canonical_rows_by_ids(org, [namespace], ids, scope=request_scope())
    return {r.canonical_id: r.memory_key for r in rows}
