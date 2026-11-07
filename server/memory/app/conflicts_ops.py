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


async def _live_peers(input: ConflictsResolveInput, peers: list[str]) -> list[str] | None:
    """The peers still live in the body's namespace, the only one a resolve reads or
    writes: a conflict is surfaced within one namespace, whose ADMIN the transport
    checked, and a namespace restored from another's snapshot keeps the memory ids.
    None when the target is not live there either."""
    import server.memory.store as memory_repo

    async def live(memory_id: str) -> bool:
        row = await memory_repo.get_by_id(
            memory_id, org=input.org, namespace=input.namespace, fresh=True, scope=WHOLE_NAMESPACE
        )
        return row is not None

    # Read from the authority: `lookup_target` reads ClickHouse, which may not hold the
    # Postgres authority's delete of the target yet.
    if not await live(input.memory_id):
        return None
    return [p for p in peers if await live(p)]


async def handle_conflicts_resolve(
    input: ConflictsResolveInput,
) -> ConflictsResolveOutput:
    """Three decision paths:

    revise:   the chosen memory wins. Peers become tombstoned.
    contract: the chosen memory is wrong. It becomes tombstoned;
              peers restored to active.
    allow:    both are legitimately distinct. Flip ALL back to active.
    """
    from server.memory.store import conflicts as conflicts_repo

    now = naive_utc_now()
    reason = (input.reason or "")[:512]
    by = (input.resolved_by or "")[:128]
    audit_note = f"resolve:{input.decision}:{reason}:{by}"

    # Look up the target + its conflict_with set; the resolve fans
    # out to either every peer or just the one named in `peer_id`.
    unresolved = ConflictsResolveOutput(
        resolved=False, decision=input.decision, affected_memory_ids=[]
    )
    target = await conflicts_repo.lookup_target(input.org, input.namespace, input.memory_id)
    if target is None:
        return unresolved
    all_peers = list(target.conflict_with)
    if input.peer_id and input.peer_id not in all_peers:
        # A stale or foreign peer id must not widen the resolve to every peer.
        return unresolved
    peers = await _live_peers(input, [input.peer_id] if input.peer_id else all_peers)
    if peers is None:
        return unresolved
    affected: list[str] = []

    # The tombstones are announced as soon as they are in, before the kept side's status
    # change: a retry finds the tombstoned side gone and would not announce it. The rows a
    # resolve keeps live change only their status, as on memory.verify: no event.
    ns = input.namespace
    if input.decision == "revise":
        tombstoned = await conflicts_repo.tombstone_ids(
            input.org, peers, audit_note, now, namespace=ns
        )
        await webhooks.announce([("memory.tombstoned", row) for row in tombstoned])
        affected.extend(peers)
        await conflicts_repo.mark_active_by_ids(input.org, [input.memory_id], namespace=ns)
        affected.append(input.memory_id)

    elif input.decision == "contract":
        tombstoned = await conflicts_repo.tombstone_ids(
            input.org, [input.memory_id], audit_note, now, namespace=ns
        )
        await webhooks.announce([("memory.tombstoned", row) for row in tombstoned])
        affected.append(input.memory_id)
        await conflicts_repo.mark_active_by_ids(input.org, peers, namespace=ns)
        affected.extend(peers)

    else:  # allow
        # Record the pair in conflict_allowlist so check_on_commit skips
        # them on future writes.
        all_ids = [input.memory_id, *peers]
        await conflicts_repo.mark_active_by_ids(input.org, all_ids, namespace=ns)
        affected.extend(all_ids)
        if peers:
            await _allow_pairs(input, peers, now=now, by=by, reason=reason or audit_note)

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
    input: ConflictsResolveInput, peers: list[str], *, now: datetime, by: str, reason: str
) -> None:
    """Allowlist the target's key against each peer's: the conflict check skips the pair
    from now on."""
    from server.memory.store import conflicts as conflicts_repo

    key_by_id = await conflicts_repo.lookup_keys_for_ids(
        input.org, [input.memory_id, *peers], namespace=input.namespace
    )
    target_key = key_by_id.get(input.memory_id)
    if not target_key:
        return
    rows = []
    for peer_id in peers:
        peer_key = key_by_id.get(peer_id)
        if not peer_key or peer_key == target_key:
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
