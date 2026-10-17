"""What the conflict verdicts of a memory.bulk_set do to the rows about to be
inserted: merged, rejected, duplicate, conflicted and refining rows, and the
first stated event date a merged row keeps. No I/O."""

from __future__ import annotations

import dataclasses
from typing import Any

from server.core.datetimes import parse_iso_dt
from server.memory.app.set_commit import _set_next, _set_peers
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
        # (new memory_id, new key, refined peers) per judged elaboration.
        self.refines: list[tuple[str, str, list[Any], str, str]] = []
        self.refined: list[str] = []
        self.reinforce_ids: list[str] = []
        self.unchecked: int = 0

    def add_refine(self, row: dict[str, Any], key: str, refined: list[Any], relation: str) -> None:
        """A refines verdict with the new row's text, so `apply_refines` can
        keep a peer the new statement would truncate."""
        self.refines.append(
            (str(row["memory_id"]), key, refined, relation, str(row.get("value") or ""))
        )


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
            relation = str(getattr(v, "refine_relation", "refines") or "refines")
            _keep_first_event_date(row, embedding_rows, emb_idx_of, idx, refined)
            plan.add_refine(row, key, refined, relation)
            plan.refined.append(key)
    elif verdict == "conflict":
        peers = [str(p.memory_id) for p in (getattr(v, "peers", None) or [])]
        if peers:
            plan.conflict_marks.append((str(row["memory_id"]), peers))
            plan.conflicted.append(key)


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


def _target_rewritten_before(v: Any, idx: int, first_row: dict[str, int]) -> bool:
    """A merge or duplicate verdict whose stored peer an earlier row of the batch
    rewrote."""
    target = str(getattr(v, "existing_memory_id", "") or getattr(v, "duplicate_of_id", "") or "")
    return bool(target) and first_row.get(target, idx) < idx


def _without_rewritten_peers(v: Any, idx: int, first_row: dict[str, int]) -> Any:
    """A refines or conflict verdict without the peers an earlier row of the batch
    rewrote: their stored bytes are gone too. Returns `v` itself when none was."""
    if getattr(v, "verdict", "") not in ("refines", "conflict"):
        return v
    old_peers = list(getattr(v, "peers", None) or [])
    old_refined = list(getattr(v, "refined_peers", None) or [])
    peers = [p for p in old_peers if first_row.get(str(p.memory_id), idx) >= idx]
    refined = [p for p in old_refined if first_row.get(str(p.memory_id), idx) >= idx]
    if len(peers) == len(old_peers) and len(refined) == len(old_refined):
        return v
    return dataclasses.replace(v, peers=peers, refined_peers=refined)


def _settling_rows(memory_rows: list[dict[str, Any]], verdicts: dict[int, Any]) -> set[int]:
    """The `id()` of each row FINAL keeps. A key written twice in one bulk is two
    versions of one row, so only the kept version's verdict may mark or retire
    peers. A merged or rejected entry writes no row."""
    dropped = {i for i, v in verdicts.items() if getattr(v, "verdict", "") in ("merge", "rejected")}
    written = [r for i, r in enumerate(memory_rows) if i not in dropped]
    return {id(r) for r in kept_versions(written).values()}


def apply_bulk_verdicts(
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    emb_idx_of: list[int],
    results: list[Any],
    verdicts: dict[int, Any],
) -> BulkVerdictPlan:
    """Pure: fold ConflictResults into the rows. `merge` and `rejected` drop
    the row (its result reports version 0); `duplicate` stamps status and
    duplicate_of on both rows; `conflict` keeps the row and records the peers
    to cross-link after the insert. Rows without a verdict pass through."""
    plan = BulkVerdictPlan()
    drop: set[int] = set()
    dup_idx: set[int] = set()
    # A verdict against a stored row an earlier entry of the batch rewrote matched bytes
    # that are gone: memory.set in the same order would find the new value there.
    first_row: dict[str, int] = {}
    for i, r in enumerate(memory_rows):
        first_row.setdefault(str(r.get("memory_id") or ""), i)
    stale = {i for i, v in verdicts.items() if _target_rewritten_before(v, i, first_row)}
    verdicts = dict(verdicts)
    for i, v in list(verdicts.items()):
        trimmed = _without_rewritten_peers(v, i, first_row)
        if trimmed is v:
            continue
        verdicts[i] = trimmed
        # Every peer it judged was rewritten: nothing left to mark or retire.
        left = trimmed.refined_peers if trimmed.verdict == "refines" else trimmed.peers
        if not left:
            stale.add(i)
    settling = _settling_rows(memory_rows, {i: v for i, v in verdicts.items() if i not in stale})
    for idx, v in verdicts.items():
        verdict = getattr(v, "verdict", "clear")
        # A later entry of this key replaced the value judged: nothing to mark or retire.
        replaced = verdict in ("refines", "conflict") and id(memory_rows[idx]) not in settling
        replaced = replaced or idx in stale
        if replaced:
            verdict = "clear"
        # The per-row verdict memory.set returns, on the bulk result too. No query: the peers
        # are the ones the check already read. A bulk merge cannot tell a first merge from a
        # repeated one without the reinforced row, so its `next` names only conflicts.
        if idx < len(results):
            _peers = _set_peers(v)
            results[idx].conflict_verdict = str(verdict or "")
            results[idx].peers = _peers
            results[idx].next = _set_next(str(verdict or ""), _peers)
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
