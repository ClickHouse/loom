"""Conflicts handlers — list + resolve."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from server.core.ch import sequential_reads
from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_value_json as _sha256
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters, scope_and
from server.memory.api.models_conflicts import (
    ConflictsAllowlistListInput,
    ConflictsAllowlistListOutput,
    ConflictsAllowlistRemoveInput,
    ConflictsAllowlistRemoveOutput,
    ConflictsListInput,
    ConflictsListOutput,
    ConflictsProposeRevisionInput,
    ConflictsProposeRevisionOutput,
    ConflictsResolveInput,
    ConflictsResolveOutput,
    _AllowlistRow,
    _ConflictCandidateMemory,
    _ConflictRevisionSuggestion,
    _ConflictRow,
)
from server.memory.app import webhooks
from server.operations.read_projection import project_rows
from server.settings import settings

log = logging.getLogger(__name__)

_count_cache: dict[tuple[str, str, ReadFilters], tuple[float, int]] = {}


def reset_conflicts_count_cache() -> None:
    """Tests, and a resolve that must be seen at once."""
    _count_cache.clear()


async def _conflicted_count_cached(
    repo: Any, org: str, namespace: str, scope: ReadFilters, *, fresh: bool = False
) -> int:
    """The count, from this replica's cache unless `fresh`; a count read fresh refreshes it."""
    key = (org, namespace, scope)
    hit = _count_cache.get(key)
    now = time.monotonic()
    if not fresh and hit is not None and now - hit[0] < settings.memory_conflicts_count_ttl_s:
        return hit[1]
    n = int(await repo.count_conflicted(org, namespace, scope=scope))
    if len(_count_cache) > 1024:
        _count_cache.clear()
    _count_cache[key] = (now, n)
    return n


async def handle_conflicts_list(
    input: ConflictsListInput,
) -> ConflictsListOutput:
    from server.memory.store import conflicts as conflicts_repo
    from server.memory.store import parse_memory_value

    scope = request_scope()
    if input.keys_only:
        # Keys for the cards' chips, no value column.
        keys = await conflicts_repo.conflicted_keys(input.org, input.namespace, scope=scope)
        return ConflictsListOutput(rows=[], keys=keys)
    if input.count_only:
        # The sidebar badge wants a number every minute from every open tab. One count per
        # (org, namespace) per `memory_conflicts_count_ttl_s` serves them all, and the count
        # reads no value column — reading the rows costs ~33 MB per tick on a large namespace.
        total = await _conflicted_count_cached(
            conflicts_repo, input.org, input.namespace, scope, fresh=input.fresh
        )
        return ConflictsListOutput(rows=[], total=total)
    rows = await conflicts_repo.list_conflicted(
        input.org,
        input.namespace,
        before_version=input.before_version,
        limit=input.limit + 1,
        scope=scope,
    )
    has_next = len(rows) > input.limit
    if has_next:
        rows = rows[: input.limit]
    out_rows = [
        _ConflictRow(
            memory_id=str(r["memory_id"]),
            memory_key=r["memory_key"],
            memory_type=r["memory_type"],
            value=parse_memory_value(r["value"]),
            version=int(r["version"]),
            pinned=bool(r["pinned"]),
            created_at=r["created_at"],
            conflict_with=[str(p) for p in r["conflict_with"]],
            content_sha256=_sha256(r["value"]) if r["value"] else "",
            sensitivity=r["sensitivity"],
        )
        for r in rows
    ]
    next_before = out_rows[-1].version if has_next and out_rows else None
    project_rows(
        out_rows, _ConflictRow, input.fields, always=frozenset({"memory_key", "sensitivity"})
    )
    return ConflictsListOutput(rows=out_rows, next_before_version=next_before)


async def handle_conflicts_propose_revision(
    input: ConflictsProposeRevisionInput,
) -> ConflictsProposeRevisionOutput:
    """Return non-destructive revise/contract/allow suggestions."""

    from server.core.ch import get_client
    from server.memory.store import conflicts as conflicts_repo

    target = await conflicts_repo.lookup_target(input.org, input.namespace, input.memory_id)
    if target is None:
        return ConflictsProposeRevisionOutput(found=False)

    peer_ids = list(target.conflict_with)
    if input.peer_id:
        peer_ids = [p for p in peer_ids if p == input.peer_id]
    ids = [input.memory_id, *peer_ids]
    if not ids:
        return ConflictsProposeRevisionOutput(found=False)

    ch = get_client()
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "ids": ids,
        "target": input.memory_id,
    }
    # The request's read scope binds these rows as it binds conflicts.list.
    in_scope = scope_and(request_scope(), params)
    rows = await ch.query_all(
        f"""
        -- `AS mid`, not `AS memory_id`: a same-name alias SHADOWS the column,
        -- so `memory_id IN {{ids:Array(UUID)}}` would compare the String alias
        -- against UUIDs and match nothing (see live_edges_for_path in
        -- server/code/store/index/edges.py).
        SELECT toString(memory_id) AS mid, memory_key, value,
               pinned, created_at
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND memory_id IN {{ids:Array(UUID)}}
          AND tombstoned_at IS NULL
          -- A high value is readable by key only, not as a revision suggestion.
          AND sensitivity != 'high'
          {in_scope}
        ORDER BY memory_id = {{target:UUID}} DESC, created_at DESC
        """,
        params=params,
    )
    if not rows:
        return ConflictsProposeRevisionOutput(found=False)

    from server.memory.store import parse_memory_value

    memories: list[_ConflictCandidateMemory] = []
    for i, r in enumerate(rows, start=1):
        value = parse_memory_value(r.get("value"))
        memories.append(
            _ConflictCandidateMemory(
                local_id=i,
                memory_id=str(r["mid"]),
                memory_key=str(r["memory_key"]),
                value=value,
                pinned=bool(r.get("pinned") or False),
                created_at=r["created_at"],
            )
        )

    target_mem = next((m for m in memories if m.memory_id == input.memory_id), None)
    if target_mem is None:
        # The target is high or outside the read scope: not found, whatever its peers are.
        return ConflictsProposeRevisionOutput(found=False)
    peer_mems = [m for m in memories if m.memory_id != target_mem.memory_id]
    peer_local_ids = [m.local_id for m in peer_mems]

    suggestions: list[_ConflictRevisionSuggestion] = []
    pinned_peers = [m for m in peer_mems if m.pinned]
    if target_mem.pinned and peer_mems:
        suggestions.append(
            _ConflictRevisionSuggestion(
                decision="revise",
                target_local_id=target_mem.local_id,
                peer_local_ids=peer_local_ids,
                rationale=(
                    "target memory is pinned, so treat it as authoritative "
                    "unless a reviewer says otherwise"
                ),
            )
        )
    elif pinned_peers:
        suggestions.append(
            _ConflictRevisionSuggestion(
                decision="contract",
                target_local_id=target_mem.local_id,
                peer_local_ids=[m.local_id for m in pinned_peers],
                rationale=(
                    "a conflicting peer is pinned, so contract the target "
                    "unless the reviewer confirms the target supersedes it"
                ),
            )
        )
    elif any(m.memory_key == target_mem.memory_key for m in peer_mems):
        suggestions.append(
            _ConflictRevisionSuggestion(
                decision="revise",
                target_local_id=target_mem.local_id,
                peer_local_ids=peer_local_ids,
                rationale=(
                    "same memory_key conflict; newest target should usually revise older peers"
                ),
            )
        )
    elif peer_mems:
        suggestions.append(
            _ConflictRevisionSuggestion(
                decision="allow",
                target_local_id=target_mem.local_id,
                peer_local_ids=peer_local_ids,
                rationale=(
                    "different memory_keys may be compatible distinctions; "
                    "reviewer should allow if both can coexist"
                ),
            )
        )

    return ConflictsProposeRevisionOutput(
        found=True,
        memories=memories,
        suggestions=suggestions,
    )


async def _head(input: ConflictsResolveInput, memory_id: str) -> Any:
    """A row's live head in the body's namespace, the only one a resolve reads or writes:
    a conflict is surfaced within one namespace, whose ADMIN or owning agent the transport
    admitted (`agents.owns`), and a namespace restored from another's snapshot keeps the
    memory ids. Read current, from the authority: the answer decides what is tombstoned,
    and ClickHouse may not hold the Postgres authority's last write of the row yet."""
    import server.memory.store as memory_repo

    return await memory_repo.get_by_id(
        memory_id, org=input.org, namespace=input.namespace, strong=True, scope=WHOLE_NAMESPACE
    )


def _awaits_decision(head: Any) -> bool:
    """The row is one side of an unresolved conflict: flagged `conflicted`, and not held
    for a proposal's review, which only `memory.proposal_decide` clears (the write fence
    refuses it here). A resolve decides no other target and writes no other peer."""
    from server.memory.domain.proposal_review import pending_review

    return str(getattr(head, "status", "") or "") == "conflicted" and not pending_review(head)


def _settled(head: Any) -> bool:
    """`mark_active_by_ids`' `skip`: a row that does not await a decision when the write
    reads it is left as it is. A row keeps its `conflict_with` once its conflict is
    settled, so a target names peers settled earlier beside the ones flagged now; a peer
    may also be a proposal on trial, and any row may have been settled since it was read."""
    return not _awaits_decision(head)


def _refuse_locked(head: Any) -> bool:
    """A lock refuses a resolve's tombstone as it refuses `memory.delete`. An agent
    settles its own namespace with a writer's token, and a resolve must not delete for it
    what `memory.delete` refuses it."""
    from server.memory.domain import lock

    lock.assert_unlocked(head, "delete", op="conflicts.resolve")
    return False


def _spared(head: Any) -> bool:
    """`tombstone_ids`' `skip`: a row that does not await a decision when the tombstone
    reads it is left as it is (`_settled`), and a lock on one that does refuses the whole
    resolve."""
    return _settled(head) or _refuse_locked(head)


async def _sides(input: ConflictsResolveInput) -> tuple[Any, dict[str, Any]] | None:
    """What a resolve acts on, decided before its first write: the target's head and the
    heads of the live peers it fans out to (every peer the target names, or the one in
    `peer_id`). None when it resolves nothing: the target is gone, is outside an
    unresolved conflict, or does not name `peer_id`.

    An ordinary fact is not a resolve's to tombstone, and a `revise` on a proposal
    awaiting review would tombstone its peers and then be refused the activation."""
    target = await _head(input, input.memory_id)
    if target is None or not _awaits_decision(target):
        return None
    named = [str(p) for p in (target.conflict_with or []) if p]
    if input.peer_id and input.peer_id not in named:
        # A stale or foreign peer id must not widen the resolve to every peer.
        return None
    heads = {p: await _head(input, p) for p in ([input.peer_id] if input.peer_id else named)}
    return target, {p: head for p, head in heads.items() if head is not None}


async def handle_conflicts_resolve(
    input: ConflictsResolveInput,
) -> ConflictsResolveOutput:
    """Three decision paths:

    revise:   the chosen memory wins. Peers become tombstoned.
    contract: the chosen memory is wrong. It becomes tombstoned;
              peers restored to active.
    allow:    both are legitimately distinct. Flip ALL back to active.

    Only rows that await a decision are written. A target outside an unresolved
    conflict resolves nothing (`_sides`), decided before anything is written. Each
    write judges the heads it reads, before it writes one: a peer that is not flagged
    is left as it is (`_settled`), and a lock refuses the tombstone (423).
    """
    from server.memory.store import conflicts as conflicts_repo

    now = naive_utc_now()
    reason = (input.reason or "")[:512]
    by = (input.resolved_by or "")[:128]
    audit_note = f"resolve:{input.decision}:{reason}:{by}"

    unresolved = ConflictsResolveOutput(
        resolved=False, decision=input.decision, affected_memory_ids=[]
    )
    sides = await _sides(input)
    if sides is None:
        return unresolved
    # `peers`: live, so `allow` allowlists each with the target. The writes below leave
    # the ones that are not flagged as they are.
    target, peers = sides
    org, ns, ids = input.org, input.namespace, list(peers)
    tombstoned: list[dict[str, Any]] = []

    # A tombstone is announced as soon as it is in. The rows a resolve keeps live change
    # only their status, as on memory.verify: no event.
    if input.decision == "revise":
        # The peers go first: after a failure of the target's status write it is still
        # flagged, and a retry finds the peers gone and completes.
        tombstoned = await conflicts_repo.tombstone_ids(
            org, ids, audit_note, now, namespace=ns, skip=_spared
        )
        await webhooks.announce([("memory.tombstoned", row) for row in tombstoned])
        kept = await conflicts_repo.mark_active_by_ids(
            org, [input.memory_id], namespace=ns, skip=_settled
        )

    elif input.decision == "contract":
        # The target goes last: a status write of the peers that fails or is refused
        # leaves every row flagged, and after a failure a retry completes. Its lock is
        # asserted first.
        _refuse_locked(target)
        kept = await conflicts_repo.mark_active_by_ids(org, ids, namespace=ns, skip=_settled)
        tombstoned = await conflicts_repo.tombstone_ids(
            org, [input.memory_id], audit_note, now, namespace=ns, skip=_spared
        )
        await webhooks.announce([("memory.tombstoned", row) for row in tombstoned])

    else:  # allow
        # Record the pair in conflict_allowlist so check_on_commit skips
        # them on future writes. The allowlist goes first: after a failure between the
        # two writes the rows are still flagged, and a retry completes.
        await _allow_pairs(input, target, peers, now=now, by=by, reason=reason or audit_note)
        kept = await conflicts_repo.mark_active_by_ids(
            org, [input.memory_id, *ids], namespace=ns, skip=_settled
        )

    affected = [str(row["memory_id"]) for row in tombstoned] + kept
    if not affected:
        return unresolved  # every row left the conflict before its write

    # The badge's cached counts (one per read scope) are stale now.
    for key in [k for k in _count_cache if k[:2] == (input.org, input.namespace)]:
        _count_cache.pop(key, None)
    total: int | None = None
    try:
        with sequential_reads():  # the count must see the writes above
            total = await _conflicted_count_cached(
                conflicts_repo, input.org, input.namespace, request_scope(), fresh=True
            )
    except Exception as e:  # noqa: BLE001 — the count never fails an applied resolve
        log.warning("conflicts.resolve: conflicted count failed for %s: %s", input.memory_id, e)
    return ConflictsResolveOutput(
        resolved=True,
        decision=input.decision,
        affected_memory_ids=affected,
        resolved_at=now,
        total=total,
    )


async def _allow_pairs(
    input: ConflictsResolveInput,
    target: Any,
    peers: dict[str, Any],
    *,
    now: datetime,
    by: str,
    reason: str,
) -> None:
    """Allowlist the target's key against each live peer's: the conflict check skips the
    pair from now on. The keys are the ones on the heads the resolve read."""
    from server.memory.store import conflicts as conflicts_repo

    target_key = str(getattr(target, "memory_key", "") or "")
    rows = []
    for head in peers.values():
        peer_key = str(getattr(head, "memory_key", "") or "")
        if not target_key or not peer_key or peer_key == target_key:
            continue
        a, b = (target_key, peer_key) if target_key < peer_key else (peer_key, target_key)
        rows.append(
            {
                "org": input.org,
                "namespace": input.namespace,
                "memory_key_a": a,
                "memory_key_b": b,
                "allowed_at": now,
                "allowed_by": by,
                "reason": reason,
                "tombstoned_at": None,
            }
        )
    await conflicts_repo.allowlist_insert(rows)


async def handle_conflicts_allowlist_list(
    input: ConflictsAllowlistListInput,
) -> ConflictsAllowlistListOutput:
    from server.memory.store import conflicts as conflicts_repo

    rows = await conflicts_repo.allowlist_list(
        input.org,
        input.namespace,
        limit=input.limit,
    )
    return ConflictsAllowlistListOutput(
        rows=[
            _AllowlistRow(
                memory_key_a=r["memory_key_a"],
                memory_key_b=r["memory_key_b"],
                allowed_at=r["allowed_at"],
                allowed_by=r["allowed_by"],
                reason=r["reason"],
            )
            for r in rows
        ]
    )


async def handle_conflicts_allowlist_remove(
    input: ConflictsAllowlistRemoveInput,
) -> ConflictsAllowlistRemoveOutput:
    """Soft-delete an allow pair via tombstoned_at. Canonicalize the
    pair so callers can pass keys in any order — the stored row has
    (min, max) regardless."""
    from server.memory.store import conflicts as conflicts_repo

    a, b = (
        (input.memory_key_a, input.memory_key_b)
        if input.memory_key_a < input.memory_key_b
        else (input.memory_key_b, input.memory_key_a)
    )
    # Check existence first so we return removed=False rather than the
    # mutation silently no-op'ing.
    if not await conflicts_repo.allowlist_exists(
        input.org,
        input.namespace,
        a,
        b,
    ):
        return ConflictsAllowlistRemoveOutput(removed=False)
    now = naive_utc_now()
    audit_note = f"unallow:{(input.reason or '')[:256]}:{(input.removed_by or '')[:128]}"
    await conflicts_repo.allowlist_remove(
        input.org,
        input.namespace,
        a,
        b,
        audit_note,
        now,
    )
    return ConflictsAllowlistRemoveOutput(removed=True)
