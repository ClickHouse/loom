"""What the conflict verdicts of a memory.bulk_set do to the rows about to be
inserted: merged, rejected, duplicate, conflicted and refining rows, and the
first stated event date a merged row keeps. No I/O."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection
from typing import Any

from server.core.datetimes import parse_iso_dt
from server.memory.app.conflicts import resolver_mark
from server.memory.app.set_commit import peer_id_in_read_scope, set_next, set_peers
from server.memory.domain.conflicts import kept_versions

# ---------------------------------------------------------------------------
# Bulk conflict surfacing
# ---------------------------------------------------------------------------


class BulkVerdictPlan:
    """What the verdicts did to the rows about to be inserted."""

    def __init__(self) -> None:
        self.memory_rows: list[dict[str, Any]] = []
        self.embedding_rows: list[dict[str, Any]] = []
        self.merged: list[str] = []
        self.rejected_pinned: list[str] = []
        self.duplicates: list[str] = []
        self.conflicted: list[str] = []
        self.conflict_marks: list[tuple[str, list[str]]] = []
        # (new memory_id, new key, replaced peers) per templated substitution.
        self.refines: list[tuple[str, str, list[Any]]] = []
        self.refined: list[str] = []
        self.reinforce_ids: list[str] = []
        # id() of a merged row -> the stored row it merged into. By row, not key: a key
        # written twice can merge twice.
        self.merged_into: dict[int, str] = {}
        self.unchecked: int = 0

    def add_refine(self, row: dict[str, Any], key: str, refined: list[Any]) -> None:
        self.refines.append((str(row["memory_id"]), key, refined))


# The verdicts that keep an entry's row out of the insert.
NOT_LANDING = ("merge", "rejected")

# A restatement dated within this many days of its peer's first date keeps its own date.
_FIRST_DATE_MAX_GAP_DAYS = 2


def _keep_first_event_date(
    row: dict[str, Any],
    embedding_rows: list[dict[str, Any]],
    emb_idx_of: list[int],
    idx: int,
    refined: list[Any],
) -> bool:
    """A restatement keeps the event's first date.

    "I recently attended a music festival in Brooklyn", said again six weeks after the
    session that first reported it, arrives anchored to its own session day at low
    confidence and refines the earlier row; without this the store would end with the event
    dated six weeks late, and an ordering question that read both dates would call it
    "recorded inconsistently". When the new row's anchor is not pinned
    (temporal_confidence < 1) and a refined peer holds an earlier `valid_at`, the new row
    takes the earliest peer date for `valid_at` and `temporal_anchor`, mirrored on its
    embedding row. Returns whether it did."""
    if float(row.get("temporal_confidence") or 0.0) >= 1.0:
        return False
    dates = [p.valid_at for p in refined if getattr(p, "valid_at", None) is not None]
    if not dates:
        return False
    earliest = min(dates)
    current = parse_iso_dt(str(row.get("valid_at") or "")) if row.get("valid_at") else None
    if current is not None and (current - earliest).days <= _FIRST_DATE_MAX_GAP_DAYS:
        return False
    # The insert writes these columns as DateTime: keep the value's type (a datetime stays a
    # datetime; only a row that already carried an ISO string gets a string). A string here
    # would fail every ingest whose new row refines an earlier-dated peer.
    when = earliest.replace(tzinfo=None)

    def _like(existing: Any) -> Any:
        return when.isoformat(timespec="seconds") if isinstance(existing, str) else when

    row["valid_at"] = _like(row.get("valid_at"))
    if "temporal_anchor" in row:
        row["temporal_anchor"] = _like(row.get("temporal_anchor"))
    if idx in emb_idx_of:
        erow = embedding_rows[emb_idx_of.index(idx)]
        if "valid_at" in erow:
            erow["valid_at"] = _like(erow.get("valid_at"))
    return True


def _fold_verdict(
    plan: BulkVerdictPlan,
    row: dict[str, Any],
    v: Any,
    *,
    idx: int,
    drop: set[int],
    dup_idx: set[int],
    embedding_rows: list[dict[str, Any]],
    emb_idx_of: list[int],
) -> None:
    """One row's verdict into the plan: a drop, a duplicate stamp, a refine
    or a conflict to cross-link."""
    key = str(row["memory_key"])
    verdict = getattr(v, "verdict", "clear")
    if verdict == "merge":
        drop.add(idx)
        plan.merged.append(key)
        if getattr(v, "existing_memory_id", ""):
            plan.reinforce_ids.append(str(v.existing_memory_id))
            plan.merged_into[id(row)] = str(v.existing_memory_id)
    elif verdict == "rejected":
        drop.add(idx)
        plan.rejected_pinned.append(key)
    elif verdict == "duplicate":
        row["status"] = "duplicate"
        row["duplicate_of"] = str(getattr(v, "duplicate_of_id", "") or "")
        dup_idx.add(idx)
        plan.duplicates.append(key)
    elif verdict == "refines":
        refined = list(getattr(v, "refined_peers", None) or [])
        if refined:
            _keep_first_event_date(row, embedding_rows, emb_idx_of, idx, refined)
            plan.add_refine(row, key, refined)
            plan.refined.append(key)
    elif verdict == "conflict":
        peers = [str(p.memory_id) for p in (getattr(v, "peers", None) or [])]
        if peers:
            plan.conflict_marks.append((str(row["memory_id"]), peers))
            plan.conflicted.append(key)
    mark = resolver_mark(v)
    if mark is not None and verdict not in NOT_LANDING and idx in emb_idx_of:
        embedding_rows[emb_idx_of.index(idx)]["conflict_checked_at"] = mark


def _kept_embeddings(
    embedding_rows: list[dict[str, Any]], emb_idx_of: list[int], drop: set[int], dup_idx: set[int]
) -> list[dict[str, Any]]:
    """The embedding rows of the rows that stay, a duplicate's stamped."""
    kept: list[dict[str, Any]] = []
    for pos, row in enumerate(embedding_rows):
        idx = emb_idx_of[pos] if pos < len(emb_idx_of) else -1
        if idx in drop:
            continue
        if idx in dup_idx:
            row["status"] = "duplicate"
        kept.append(row)
    return kept


def verdict_target(v: Any) -> str:
    """The stored row a merge or duplicate verdict matched, else ''."""
    return str(getattr(v, "existing_memory_id", "") or getattr(v, "duplicate_of_id", "") or "")


def _without_rewritten_peers(v: Any, landing_ids: set[str]) -> Any:
    """A refines or conflict verdict without the peers a landing row of the batch
    rewrites. An earlier row replaced the bytes judged; a later one would have its new
    value retired or flagged, since the marks run after the insert. A peer is never the
    row's own id: the conflict check skips the writer's key. Returns `v` itself when no
    peer was dropped."""
    if getattr(v, "verdict", "") not in ("refines", "conflict"):
        return v
    old_peers = list(getattr(v, "peers", None) or [])
    old_refined = list(getattr(v, "refined_peers", None) or [])
    peers = [p for p in old_peers if str(p.memory_id) not in landing_ids]
    refined = [p for p in old_refined if str(p.memory_id) not in landing_ids]
    if len(peers) == len(old_peers) and len(refined) == len(old_refined):
        return v
    return dataclasses.replace(v, peers=peers, refined_peers=refined)


def _settling_rows(memory_rows: list[dict[str, Any]], landed: Collection[int]) -> set[int]:
    """The `id()` of each row FINAL keeps. A key written twice in one bulk is two
    versions of one row, so only the kept version's verdict may mark or retire
    peers. Only the `landed` rows are written."""
    written = [r for r in memory_rows if id(r) in landed]
    return {id(r) for r in kept_versions(written).values()}


def _stamp_result(result: Any, v: Any, verdict: str) -> None:
    """The per-row verdict memory.set returns, on the bulk result too. No query: the peers
    are the ones the check already read. A bulk merge cannot tell a first merge from a
    repeated one without the reinforced row, so its `next` names only conflicts. A
    rejection names its pinned blocker, as memory.set's 409 does."""
    peers = set_peers(v)
    result.conflict_verdict = verdict
    result.peers = peers
    result.next = set_next(verdict, peers)
    if verdict == "rejected":
        blocker = str(getattr(v, "pinned_blocker_id", "") or "")
        result.pinned_blocker_id = peer_id_in_read_scope(v, blocker)


def apply_bulk_verdicts(
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    emb_idx_of: list[int],
    results: list[Any],
    verdicts: dict[int, Any],
    *,
    stale_rows: Collection[int],
    landed_rows: Collection[int],
) -> BulkVerdictPlan:
    """Pure: fold ConflictResults into the rows. `merge` and `rejected` drop
    the row (its result reports version 0); `duplicate` stamps status and
    duplicate_of on both rows; `conflict` keeps the row and records the peers
    to cross-link after the insert. Rows without a verdict pass through.
    `stale_rows` are the `id()` of the rows whose verdict is stale
    (`settle_steps`): they land as clear. `landed_rows` are the `id()` of the rows
    that land (`settle_steps`); a peer one of them rewrites is not marked or retired."""
    plan = BulkVerdictPlan()
    drop: set[int] = set()
    dup_idx: set[int] = set()
    stale = {i for i in verdicts if id(memory_rows[i]) in stale_rows}
    landing_ids = {str(r.get("memory_id") or "") for r in memory_rows if id(r) in landed_rows}
    verdicts = dict(verdicts)
    for i, v in list(verdicts.items()):
        trimmed = _without_rewritten_peers(v, landing_ids)
        if trimmed is v:
            continue
        verdicts[i] = trimmed
        # Every peer it judged was rewritten: nothing left to mark or retire.
        left = trimmed.refined_peers if trimmed.verdict == "refines" else trimmed.peers
        if not left:
            stale.add(i)
    settling = _settling_rows(memory_rows, landed_rows)
    for idx, v in verdicts.items():
        verdict = getattr(v, "verdict", "clear")
        # A later entry of this key replaced the value judged: nothing to mark or retire.
        replaced = verdict in ("refines", "conflict") and id(memory_rows[idx]) not in settling
        replaced = replaced or idx in stale
        if replaced:
            verdict = "clear"
        if idx < len(results):
            # A verdict demoted to clear names no peers, as memory.set's demotion does.
            _stamp_result(results[idx], None if replaced else v, str(verdict or ""))
        if replaced:
            continue
        _fold_verdict(
            plan,
            memory_rows[idx],
            v,
            idx=idx,
            drop=drop,
            dup_idx=dup_idx,
            embedding_rows=embedding_rows,
            emb_idx_of=emb_idx_of,
        )
    for idx in drop:
        if idx < len(results):
            results[idx].version = 0
            results[idx].memory_id = ""
    # ch.insert derives the column list from rows[0]: a `duplicate` verdict stamping
    # status/duplicate_of on SOME rows would make the key sets uneven and fail the whole batch
    # with KeyError('status'). Every row carries both keys; the defaults are the columns' own
    # ('active', '').
    for row in memory_rows:
        row.setdefault("status", "active")
        row.setdefault("duplicate_of", "")
    plan.memory_rows = [r for i, r in enumerate(memory_rows) if i not in drop]
    plan.embedding_rows = _kept_embeddings(embedding_rows, emb_idx_of, drop, dup_idx)
    plan.unchecked = max(len(emb_idx_of) - len(verdicts), 0) if verdicts else 0
    return plan
