"""Superseded rows are served only when asked.

A later write that replaces a fact retires the earlier row as
`status='superseded'` with `superseded_by` naming the successor; the read
predicates do not admit those rows (the served set is `active` +
`conflicted`). That is right for "what is X" and wrong for "what did we use
before" and "why did X change", where the only other read that sees a retired
value is `memory.history`, by key.

This module is the read-side rule, the mirror of `served_duplicates`: a
search, ask or list carries no `superseded` row unless the call asks
(`include_superseded`); when it does, each superseded hit names its successor
(`superseded_by`, the key when the successor row is live, else its id) and
the moment it was retired (`superseded_at`), read in one keyed statement over
`loom.memory`. The mirror's own `status` is trusted for the predicate: the
supersession write re-emits both tables (`supersede_duplicates` in the
repository), so the `memory_embeddings` rows behind superseded memories carry
`superseded` and every superseded row names a successor. The successor
read stays inside the request's read scope, like `served_duplicates`.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from server.identity.app.request_scope import request_scope
from server.memory.domain.search_types import hit_row_key, names_row, row_key

log = logging.getLogger(__name__)


class ServedFlags(TypedDict):
    """The widening flags a leg predicate takes."""

    include_duplicates: bool
    include_superseded: bool
    include_proposed: bool


def hide_superseded(input: Any) -> bool:
    """Whether this read hides `superseded` rows: always, unless the caller
    asked for them (`include_superseded`)."""
    return not bool(getattr(input, "include_superseded", False))


def served_flags(input: Any) -> ServedFlags:
    """The two widening flags a leg predicate takes, from one request: the
    keyword form `served_status_sql` / `SearchQuery` accept."""
    from server.memory.app.served_duplicates import hide_duplicates

    return ServedFlags(
        include_duplicates=not hide_duplicates(input),
        include_superseded=not hide_superseded(input),
        # Proposals are served only to a read that asks; no
        # deployment switch serves them to everyone.
        include_proposed=bool(getattr(input, "include_proposed", False)),
    )


def _is_superseded_fact(h: Any) -> bool:
    return (
        str(getattr(h, "status", "") or "") == "superseded"
        and (getattr(h, "source", "fact") or "fact") == "fact"
        and not str(getattr(h, "memory_key", "") or "").startswith("chunk:")
    )


def superseded_label(m: Any) -> str:
    """The inline note a superseded memory line carries so a reader states
    the value as history, never as current: `[superseded <date>; replaced by
    <key>]` — the date and the successor only when the row knows them."""
    if str(getattr(m, "status", "") or "") != "superseded":
        return ""
    at = str(getattr(m, "superseded_at", "") or "")[:10]
    by = str(getattr(m, "superseded_by", "") or "")
    parts = ["superseded" + (f" {at}" if at else "")]
    if by:
        parts.append(f"replaced by {by}")
    return "[" + "; ".join(parts) + "]"


async def annotate_pool_superseded(ctx: Any) -> None:
    """Name the successor on every superseded hit in the pool that names its row (one
    keyed statement over `loom.memory`, only when the call asked for superseded rows and
    one is in the pool) and count them in `retrieval_work` as `superseded_served`. A hit
    that names no row could match none. Fail-open: a failed read leaves the hits with their
    `status` and nothing else."""
    input = ctx.input
    if hide_superseded(input):
        return
    old = [h for h in ctx.all_hits if _is_superseded_fact(h) and names_row(h)]
    if not old:
        return
    ctx.actual_work["superseded_served"] = len(old)
    ns_default = str(input.namespace)
    scope = request_scope()
    try:
        import server.memory.store as memory_repo

        rows = await memory_repo.superseded_successors(
            input.org, [hit_row_key(h, ns_default) for h in old], scope=scope
        )
    except Exception as e:  # noqa: BLE001 — naming the successor never fails a search
        log.warning("memory.search: superseded successor read failed: %s", e)
        return
    by_row = {row_key(r): r for r in rows}
    for h in old:
        r = by_row.get(hit_row_key(h, ns_default))
        if r is None:
            continue
        # A bounded read sees the raw id only when the successor resolved inside
        # its scope; otherwise the id may name another user's row.
        h.superseded_by = r.successor_key or ("" if scope.narrowed else r.superseded_by)
        h.superseded_at = r.superseded_at


async def shown_successors(org: str, namespace: str, rows: list[Any]) -> dict[str, str]:
    """memory_id -> the `superseded_by` id a read may show for each row. A read bounded
    to one user or session sees a successor id only when that row resolves inside its
    scope: otherwise the id may name another user's row, and gone and foreign look alike."""
    from server.identity.app.request_scope import request_narrowed

    ids = {str(r.memory_id): str(getattr(r, "superseded_by", "") or "") for r in rows}
    if not request_narrowed() or not any(ids.values()):
        return ids
    import server.memory.store as memory_repo

    # Any live status: a successor that is itself superseded is still a row this read sees.
    seen = await memory_repo.live_ids_in_scope(
        org, [namespace], [s for s in ids.values() if s], scope=request_scope()
    )
    return {mid: (s if s in seen else "") for mid, s in ids.items()}
