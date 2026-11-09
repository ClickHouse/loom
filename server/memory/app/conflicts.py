"""Semantic conflict detection on memory.set.

1. Vector-search active peers in the same (org, namespace, memory_type)
   at cosineDistance ≤ (1 - threshold).
2. Canonical-normalize both texts; exact match → no-op restatement,
   return existing memory_id, no insert.
3. Embeddings close, normalized forms differ → real conflict. Caller
   inserts and flags both rows status='conflicted' + conflict_with.
4. Pinned short-circuit: a non-pinned write conflicting with a pinned
   peer is rejected with 409 PINNED_CONFLICT — pinned is a write-time
   invariant, not a UI badge.

A KEY_IDENTIFIED_TYPES row (a decision) skips the merge, duplicate and
refines steps and the pinned short-circuit: a row under another key is
another decision, so every close peer is only a conflict.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.core.ch import get_client
from server.core.errors import AuthInsufficientError, IdentityIncompleteError
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import (
    WHOLE_NAMESPACE,
    ReadFilters,
    row_in_read_scope,
    scope_and,
)
from server.memory.domain.conflicts import (
    APPEND_ONLY_POLICY,
    ConflictPeer,
    ConflictResult,
    WriteConfig,
    _as_datetime,
    canonical,
    merge_or_run_duplicate,
    query_max_distance,
    same_content_words,
    substitution_result,
    templated_substitution,
)
from server.memory.domain.memory_types import KEY_IDENTIFIED_TYPES
from server.memory.domain.temporal_sql import LIVE_STATUSES
from server.memory.store import conflicts as _conflicts_repo
from server.memory.store.conflict_candidates import (
    _PEER_ROWS_SQL,
    PEER_LIMIT,
    PINNED_PEERS_SQL,
    _peer_candidates_sql,
)
from server.settings import settings

if TYPE_CHECKING:
    from server.memory.app.auto_resolution import Follow

log = logging.getLogger(__name__)


DEFAULT_CONFLICT_POLICY = "auto_reinforce"


async def _threshold_from(
    type_cfg: Any, ns_row: Mapping[str, Any], org: str, namespace: str
) -> float:
    """Conflict threshold precedence: per-type dedup threshold > namespace
    threshold > embedder calibration."""
    threshold = getattr(type_cfg, "dedup_threshold", None)
    if threshold is None:
        threshold = ns_row.get("conflict_threshold")
    if threshold is None:
        from server.memory.app.embedder_calibration import effective_conflict_threshold_for_ns

        return await effective_conflict_threshold_for_ns(
            settings.conflict_threshold, org, namespace
        )
    return float(threshold)


async def _dup_threshold_from(ns_row: Mapping[str, Any], org: str, namespace: str) -> float:
    """Duplicate-tier threshold precedence: namespace > embedder calibration.
    It sits above the conflict threshold."""
    dup = ns_row.get("duplicate_threshold")
    if dup is None:
        from server.memory.app.embedder_calibration import effective_duplicate_threshold_for_ns

        return await effective_duplicate_threshold_for_ns(
            settings.duplicate_threshold, org, namespace
        )
    return float(dup)


def _policy_from(ns_row: Mapping[str, Any]) -> str:
    return str(ns_row.get("default_conflict_policy") or DEFAULT_CONFLICT_POLICY)


async def _resolve_threshold(org: str, namespace: str, memory_type: str | None = None) -> float:
    """The conflict threshold for one type, read on its own (a write that did
    not prefetch its `WriteConfig`)."""
    from server.memory.app.namespace_types import get_type_config

    type_cfg = await get_type_config(org, namespace, memory_type or "")
    return await _threshold_from(
        type_cfg,
        await _conflicts_repo.namespace_write_row(org, namespace, ch=get_client()),
        org,
        namespace,
    )


async def _resolve_duplicate_threshold(org: str, namespace: str) -> float:
    row = await _conflicts_repo.namespace_write_row(org, namespace, ch=get_client())
    return await _dup_threshold_from(row, org, namespace)


async def _allowlisted_keys(org: str, namespace: str, my_key: str) -> set[str]:
    """Keys allow-listed against `my_key`: a pair (a, b) in
    conflict_allowlist means a write to either treats the other as no
    conflict even though the embeddings are close."""
    return await _conflicts_repo.allowlisted_peers(org, namespace, my_key, ch=get_client())


def _peer_query_distance(threshold: float) -> float:
    """The distance the peer statement reaches: the conflict threshold, or with the
    conflict resolver on its lower floor, so a write with a peer in between is
    marked for the resolver (those peers never flag on cosine alone)."""
    if settings.memory_conflict_resolver_enabled:
        threshold = min(threshold, float(settings.memory_conflict_resolver_min_similarity))
    return query_max_distance(threshold)


def resolver_mark(result: Any) -> datetime | None:
    """The `conflict_checked_at` a write's vector row carries: the conflict resolver's
    owed mark when the check asked for a judgement, else NULL."""
    from server.memory.store.conflict_resolver import OWED_MARK

    if settings.memory_conflict_resolver_enabled and bool(getattr(result, "review", False)):
        return OWED_MARK
    return None


async def same_key_mark(
    result: Any, *, org: str, namespace: str, memory_row: dict[str, Any], policy: str | None
) -> datetime | None:
    """`resolver_mark` for a point write, or the owed mark when another session of the
    same subject holds the row's key live: a shared key is not proof of one attribute,
    so the resolver judges the pair (as bulk writes do). Not under `newest_wins`, which
    retires the older row itself, nor `append_only`, nor for the anonymous user
    (`keys_held_elsewhere`). One query; fail-open."""
    from server.memory.app.key_supersession import keys_held_elsewhere
    from server.memory.store.conflict_resolver import OWED_MARK

    mark = resolver_mark(result)
    if mark is not None or not settings.memory_conflict_resolver_enabled:
        return mark
    try:
        ch = get_client()
        policy = policy or await namespace_conflict_policy(org, namespace, ch=ch)
        if policy in ("newest_wins", "append_only"):
            return None
        held = await keys_held_elsewhere(
            org=org, namespace=namespace, memory_rows=[memory_row], ch=ch
        )
    except Exception as e:  # noqa: BLE001 — the row lands unmarked; the write stands
        log.warning("same-key mark skipped (non-fatal): %s", e)
        return None
    return OWED_MARK if str(memory_row.get("memory_key") or "") in held else None


async def resolve_conflict_floor(org: str, namespace: str, memory_type: str) -> tuple[float, float]:
    """(threshold, max_distance) for one namespace and memory_type, as `check_on_commit`
    resolves them: for a caller that reads them once for many checks, or needs them
    without one."""
    threshold = await _resolve_threshold(org, namespace, memory_type=memory_type)
    return threshold, _peer_query_distance(threshold)


async def _policy_before_gate(resolved_policy: str | None, org: str, namespace: str) -> str | None:
    """The namespace policy the gate consults before its peer
    statement. Under `append_only` the namespace's writes are events, not
    claims — a run's report is not a restatement of last run's because it
    shares the template — so every write is its own row: no merge, duplicate
    or conflict verdict against a peer (a same-key rewrite stays
    last-writer-wins by version) and the peer statement is never issued.
    The prefetched policy when the caller had it, else one point read; an
    unreadable policy keeps the cosine path (None)."""
    if resolved_policy is not None:
        return resolved_policy
    try:
        return await namespace_conflict_policy(org, namespace, ch=get_client())
    except Exception as e:  # noqa: BLE001 — unknown policy keeps the gate
        logging.getLogger(__name__).warning(
            "conflict gate: policy read failed (%s); the gate runs", e
        )
        return None


_PEER_FIRST_K = 32
_PEER_MAX_K = 2048


async def nearest_peer_rows(
    ch: Any,
    params: dict[str, Any],
    scope: ReadFilters = WHOLE_NAMESPACE,
    *,
    limit: int = PEER_LIMIT,
) -> list[dict[str, Any]]:
    """The gate's peers: this fact's cosine neighbours under the same type,
    excluding its own key (a re-write is LWW by version, not a conflict), at
    most `limit` (default `PEER_LIMIT`), nearest first.

    Two statements instead of the `memory FINAL JOIN memory_embeddings FINAL` scan.
    Stage A ranks the namespace's vectors alone, in the shape the `Quantized` codec
    answers codes-first (a bare distance ORDER BY + LIMIT and
    `vector_search_settings()`), guarded on `embedding.size0` — `length(embedding)`
    decodes the full-precision column and turns codes-first off (read volume roughly 4x
    higher). Stage B reads the candidates' `loom.memory` rows by sort key and applies
    the row-side filters (type, status, tombstone), which is where the two tables can
    disagree (vectors of non-live rows, live rows whose vector says otherwise). Stage A
    filters nothing the join did not filter on the vector side. When fewer than
    `limit` survive while stage A's last candidate is still inside `max_dist`,
    stage A widens (x4) — the answer is the join's, not an approximation of it. Ties
    sort by memory_key (the join's order among equal distances is arbitrary).
    Both stages stay inside `scope`, so other users' rows never take the places."""
    params = dict(params)
    in_scope = scope_and(scope, params)
    k = _PEER_FIRST_K
    while True:
        cand = await ch.query_all(_peer_candidates_sql(in_scope), params={**params, "k": k})
        inside = [c for c in cand if float(c["dist"]) <= float(params["max_dist"])]
        live: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        if inside:
            rows = await ch.query_all(
                _PEER_ROWS_SQL,
                params={**params, "ids": [_ident(c) for c in inside]},
            )
            live = {_ident(r): r for r in rows}
        out = [{**live[_ident(c)], "dist": float(c["dist"])} for c in inside if _ident(c) in live]
        exhausted = len(cand) < k or len(inside) < len(cand)
        if len(out) >= limit or exhausted:
            break
        if k >= _PEER_MAX_K:
            return await _peer_rows_joined(ch, params, scope_and(scope, params, "m"), limit)
        k *= 4
    out.sort(key=lambda r: (r["dist"], str(r.get("memory_key") or "")))
    return out[:limit]


def _ident(r: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(r.get("agent") or ""),
        str(r.get("user_id") or ""),
        str(r.get("session_id") or ""),
        str(r.get("memory_key") or ""),
    )


async def _peer_rows_joined(
    ch: Any, params: dict[str, Any], in_scope: str = "", limit: int = PEER_LIMIT
) -> list[dict[str, Any]]:
    """The single-statement join: the widening's last resort, once stage A
    has read `_PEER_MAX_K` candidates without enough live peers. Filtered to
    equal-length vectors so cosineDistance never sees mixed embedders
    (SIZES_OF_ARRAYS)."""
    rows: list[dict[str, Any]] = await ch.query_all(
        """
        SELECT
            m.memory_id, m.memory_key, m.value, m.pinned,
            m.agent, m.user_id, m.session_id, m.run_id, m.valid_at,
            """
        + "cosineDistance(e.embedding, {emb:Array(Float32)})"
        + """ AS dist
        FROM loom.memory m FINAL
        JOIN loom.memory_embeddings e FINAL
          ON  e.org = m.org AND e.namespace = m.namespace
          AND e.memory_key = m.memory_key
          AND e.user_id = m.user_id AND e.session_id = m.session_id
          AND e.agent = m.agent
        WHERE m.org = {org:String}
          AND m.namespace = {ns:String}
          AND m.memory_type = {mt:String}
          AND m.memory_key != {self_key:String}
          AND m.tombstoned_at IS NULL
          AND m.status IN ('active', 'conflicted')
          AND e.tombstoned_at IS NULL
          AND e.embedding_dim = {dim:UInt16}
          AND length(e.embedding) = {dim:UInt16}"""
        + in_scope
        + f"""
        HAVING dist <= {{max_dist:Float32}}
        ORDER BY dist ASC
        LIMIT {int(limit)}
        """,
        params=params,
    )
    return rows


def _scope_or_none() -> ReadFilters | None:
    """The request's read scope, None when it does not resolve (no peer is inside)."""
    try:
        return request_scope()
    except (AuthInsufficientError, IdentityIncompleteError):
        return None


def _foreign_pinned(r: Mapping[str, Any], scope: ReadFilters | None) -> bool:
    """A pinned row outside `scope` (every row is outside a scope that did not resolve).
    The writer's own rows come from its scoped read, with the gate's 10 places."""
    if not r.get("pinned"):
        return False
    return scope is None or not row_in_read_scope(scope, r.get("user_id"), r.get("session_id"))


async def _foreign_pinned_rows(
    ch: Any, params: dict[str, Any], scope: ReadFilters | None, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The pinned peers the scoped peer query cannot see, for a writer bounded
    below the namespace: a pinned row of another user still blocks its write.
    Its own statement (`PINNED_PEERS_SQL`), so the writer's closer rows never hide a
    pinned one, and the pinned memory rows decide type, not their vectors' metadata."""
    if scope is not None and not scope.narrowed:
        return []
    seen = {str(r.get("memory_id")) for r in rows}
    pinned = await ch.query_all(PINNED_PEERS_SQL, params=params)
    return [
        dict(r) for r in pinned if _foreign_pinned(r, scope) and str(r.get("memory_id")) not in seen
    ]


def _split_by_read_scope(
    peers: list[ConflictPeer],
) -> tuple[list[ConflictPeer], list[ConflictPeer]]:
    """(peers inside the request's read scope, peers outside it). A writer
    bounded to one user never merges into, reinforces, supersedes or marks
    another user's row; only a pinned one still blocks it. A scope the
    request cannot resolve keeps no peer inside."""
    scope = _scope_or_none()
    if scope is None:
        return [], peers
    inside: list[ConflictPeer] = []
    outside: list[ConflictPeer] = []
    for p in peers:
        (inside if row_in_read_scope(scope, p.user_id, p.session_id) else outside).append(p)
    return inside, outside


def _peer_of(r: dict[str, Any]) -> ConflictPeer:
    """A candidate row as a ConflictPeer (similarity from its cosine distance)."""
    return ConflictPeer(
        memory_id=str(r["memory_id"]),
        memory_key=r["memory_key"],
        similarity=1.0 - float(r["dist"]),
        pinned=bool(r["pinned"]),
        canonical=canonical(r["value"]),
        agent=str(r.get("agent") or ""),
        user_id=str(r.get("user_id") or ""),
        session_id=str(r.get("session_id") or ""),
        value=str(r.get("value") or ""),
        run_id=str(r.get("run_id") or "-"),
        valid_at=_as_datetime(r.get("valid_at")),
    )


async def _substitution_verdict(
    peers: list[ConflictPeer],
    *,
    new_value: str,
    new_valid_at: datetime | None,
    resolved_policy: str | None,
    org: str,
    namespace: str,
) -> ConflictResult | None:
    """The verdict for peers that are templated substitutions of the new value
    under `newest_wins`, or None when there are none or the policy is another."""
    # The shape alone pairs two attributes of one subject ("X is 28 years old" /
    # "X is a software engineer"); the update shape must also hold: the same
    # subject and predicate, a different object.
    from server.memory.domain.triples_follow_rows import object_substitution

    subst = [
        p
        for p in peers
        if templated_substitution(new_value, p.value) and object_substitution(p.value, new_value)
    ]
    if not subst:
        return None
    try:
        policy = (
            resolved_policy
            if resolved_policy is not None
            else await namespace_conflict_policy(org, namespace, ch=get_client())
        )
    except Exception:  # noqa: BLE001 — unknown policy keeps the cosine path
        policy = ""
    if policy != "newest_wins":
        return None
    return substitution_result(subst, peers, new_valid_at=new_valid_at)


async def check_on_commit(
    *,
    org: str,
    namespace: str,
    agent: str,
    memory_type: str,
    memory_key: str,
    new_value: str,
    new_embedding: list[float],
    new_run_id: str = "-",
    new_valid_at: datetime | None = None,
    candidate_rows: list[dict[str, Any]] | None = None,
    resolved_threshold: float | None = None,
    resolved_duplicate_threshold: float | None = None,
    resolved_policy: str | None = None,
    allowlisted_keys: set[str] | None = None,
) -> ConflictResult:
    """Run the 4-step algorithm. Caller raises 409 on verdict=='rejected'
    and applies merge/conflict flags on insert.

    `resolved_duplicate_threshold` / `resolved_policy` / `allowlisted_keys`: the
    namespace reads a caller already made (a point write's `prefetch_write_config`, a
    bulk call's one policy read); None reads them here.

    `new_run_id`: a write made under a run keeps its own row.
    A canonical-form match with a peer from another run, or from no run,
    is returned as `duplicate` (insert, status='duplicate', `duplicate_of`
    the peer) rather than `merge` (no insert), so the run's `run_id`-scoped
    reads find what the run wrote. Same-run restatements still merge.

    `new_valid_at`: the new row's validity date when the writer supplied one; it decides
    which side of a templated substitution `newest_wins` retires.

    `candidate_rows`: the peers a caller already read (`peer_rows`, under the Postgres
    authority); the peer and pinned statements are skipped. `resolved_threshold`: the
    type's conflict threshold, when the caller read it; the lookups are skipped."""
    # Non-searchable types (episodic) carry no embedding → skip cleanly.
    if not new_embedding:
        return ConflictResult(verdict="clear", peers=[])

    resolved_policy = await _policy_before_gate(resolved_policy, org, namespace)
    if resolved_policy == APPEND_ONLY_POLICY:  # no verdict against a peer
        return ConflictResult(verdict="clear", peers=[])

    threshold = (
        float(resolved_threshold)
        if resolved_threshold is not None
        else await _resolve_threshold(org, namespace, memory_type=memory_type)
    )
    max_distance = _peer_query_distance(threshold)

    # Excludes our own memory_key (re-write is LWW by version, not a conflict)
    # and filters peers by embedding_dim so cosineDistance gets equal-length
    # arrays — mixed-embedder rows in a namespace else raise SIZES_OF_ARRAYS.
    ch = get_client()
    new_dim = len(new_embedding)
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "mt": memory_type,
        "self_key": memory_key,
        "emb": new_embedding,
        "max_dist": max_distance,
        "dim": new_dim,
    }
    scope = _scope_or_none()
    if candidate_rows is not None:
        rows = [dict(r) for r in candidate_rows]
    elif scope is None:
        rows = []
    else:
        rows = await nearest_peer_rows(ch, params, scope)
    keyed = memory_type in KEY_IDENTIFIED_TYPES
    # Rows passed in (`peer_rows`) carry their pinned arm already; a decision needs none.
    if candidate_rows is None and not keyed:
        rows += await _foreign_pinned_rows(ch, params, scope, rows)
    rows = await _without_erased(org, namespace, rows)
    if not rows:
        return ConflictResult(verdict="clear", peers=[])

    # Allowlisted pairs (decision='allow') coexist by operator decision —
    # drop them before canonical/pinned checks so they're never re-flagged.
    allowlisted = (
        allowlisted_keys
        if allowlisted_keys is not None
        else await _allowlisted_keys(org, namespace, memory_key)
    )
    rows = [r for r in rows if r["memory_key"] not in allowlisted]
    if not rows:
        return ConflictResult(verdict="clear", peers=[])

    all_peers, foreign = _split_by_read_scope([_peer_of(r) for r in rows])
    peers = [p for p in all_peers if p.similarity >= threshold]
    if keyed:
        verdict = "conflict" if peers else "clear"
        return ConflictResult(verdict=verdict, peers=peers, review=bool(all_peers))
    new_canonical = canonical(new_value)
    # A pinned row outside the writer's read scope still blocks it.
    foreign_blocker = next((p for p in foreign if p.pinned and p.similarity >= threshold), None)
    if not peers and foreign_blocker is None:
        # Peers below the threshold only ask the conflict resolver to look.
        return ConflictResult(verdict="clear", peers=[], review=bool(all_peers))

    # Step 2 — canonical-form match → merge (return existing id, skip insert), unless
    # the write is run-scoped and the peer is not this run's: then the row is inserted
    # as the run's duplicate of the peer.
    for p in peers:
        if p.canonical == new_canonical:
            return merge_or_run_duplicate(p, peers, new_run_id=new_run_id)

    # Step 4 (checked before 3 — pinned short-circuit takes priority).
    pinned_blocker = next((p for p in peers if p.pinned), None) or foreign_blocker
    if pinned_blocker is not None:
        return ConflictResult(
            verdict="rejected",
            peers=peers,
            pinned_blocker_id=pinned_blocker.memory_id,
        )

    # Step 3 (before 3a) — a templated substitution under `newest_wins`: the
    # same statement shape with a different fill is an update to a
    # single-valued attribute, and the policy says the newer statement wins.
    # Deterministic.
    substituted = await _substitution_verdict(
        peers,
        new_value=new_value,
        new_valid_at=new_valid_at,
        resolved_policy=resolved_policy,
        org=org,
        namespace=namespace,
    )
    if substituted is not None:
        return substituted

    # Step 3a — similarity ≥ duplicate_threshold, canonical differs: same
    # fact, different words. No contradiction, so 'duplicate' not 'conflict'.
    # "Same fact" has to mean the same content words: "works in the field of
    # politician" and "works in the field of soldier" sit above the duplicate
    # gate (one token apart in a templated sentence); filing the second as a
    # duplicate of the first would lose the update. A pair whose content words differ
    # falls through to the conflict flag.
    dup_threshold = (
        float(resolved_duplicate_threshold)
        if resolved_duplicate_threshold is not None
        else await _resolve_duplicate_threshold(org, namespace)
    )
    dup_peer = next(
        (
            p
            for p in peers
            if p.similarity >= dup_threshold and same_content_words(new_value, p.value)
        ),
        None,
    )
    if dup_peer is not None:
        return ConflictResult(
            verdict="duplicate",
            peers=peers,
            duplicate_of_id=dup_peer.memory_id,
        )

    # Step 3b — close-but-different (below duplicate_threshold but
    # above conflict threshold) → conflict, for the conflict resolver to judge.
    return ConflictResult(verdict="conflict", peers=peers, review=True)


async def namespace_conflict_policy(org: str, namespace: str, *, ch: Any) -> str:
    """The namespace's `default_conflict_policy`, or the default when unset."""
    return _policy_from(await _conflicts_repo.namespace_write_row(org, namespace, ch=ch))


async def prefetch_write_config(
    org: str, namespace: str, memory_type: str, memory_key: str
) -> WriteConfig:
    """The type row, the namespace row and the allowlist for this key in one
    concurrent round trip, resolved by the same `_threshold_from` /
    `_dup_threshold_from` / `_policy_from` the per-read helpers use. Never
    raises: a failed prefetch returns an empty `WriteConfig` and the write
    reads each value it needs on its own."""
    import asyncio

    from server.memory.app.namespace_types import get_type_config

    try:
        type_cfg, ns_row, allow = await asyncio.gather(
            get_type_config(org, namespace, memory_type),
            _conflicts_repo.namespace_write_row(org, namespace, ch=get_client()),
            _allowlisted_keys(org, namespace, memory_key),
        )
        threshold = await _threshold_from(type_cfg, ns_row, org, namespace)
        dup = await _dup_threshold_from(ns_row, org, namespace)
    except Exception as e:  # noqa: BLE001 — the write reads per statement instead
        log.warning("write config prefetch failed (per-read fallback): %s", e)
        return WriteConfig()
    return WriteConfig(threshold, dup, _policy_from(ns_row), set(allow), type_cfg, memory_type)


def start_write_config(org: str, namespace: str, memory_type: str, memory_key: str) -> Any:
    """Schedule `prefetch_write_config` so it overlaps the write's other
    reads; the caller awaits the task where it needs the values."""
    import asyncio

    return asyncio.ensure_future(prefetch_write_config(org, namespace, memory_type, memory_key))


async def type_config_for(cfg_task: Any, ident: Any, memory_type: str) -> Any:
    """The type row the prefetch already read when it is this type's; else
    one read (the retention lookup may use the pre-retype type)."""
    cfg = await cfg_task
    if cfg.type_config is not None and cfg.memory_type == memory_type:
        return cfg.type_config
    from server.memory.app.namespace_types import get_type_config

    return await get_type_config(ident.org, ident.namespace, memory_type)


async def auto_reinforce_peer(ident: Any, peer_id: str, *, ch: Any, cfg: Any = None) -> None:
    """The `merge` verdict's evidence bump on the restated row: under the
    type's conflict policy, else the namespace's (`auto_reinforce` default),
    one `evidence` usage event on the peer. Fail-open; never raises. `ident`
    carries org / namespace / memory_type / agent; `cfg` a `WriteConfig`
    whose type row and policy are reused when present."""
    try:
        import server.memory.store as memory_repo

        type_cfg = getattr(cfg, "type_config", None)
        if type_cfg is None or getattr(cfg, "memory_type", "") != ident.memory_type:
            from server.memory.app.namespace_types import get_type_config

            type_cfg = await get_type_config(ident.org, ident.namespace, ident.memory_type)
        policy = type_cfg.conflict_policy or getattr(cfg, "policy", None)
        if not policy:
            policy = await namespace_conflict_policy(ident.org, ident.namespace, ch=ch)
        if policy == DEFAULT_CONFLICT_POLICY and peer_id:
            await memory_repo.record_usage_event(
                memory_id=peer_id,
                org=ident.org,
                namespace=ident.namespace,
                event_type="evidence",
                event_count=1,
                agent=ident.agent,
            )
    except Exception as e:  # noqa: BLE001 — the evidence bump is best effort; the write stands
        logging.getLogger(__name__).warning("auto_reinforce bump failed (non-fatal): %s", e)


async def apply_refines(
    *,
    org: str,
    namespace: str,
    new_memory_id: str,
    new_memory_key: str,
    refined_peers: list[ConflictPeer],
    ch: Any = None,
    record_into: dict[str, list[dict[str, Any]]] | None = None,
    expected_sha256: str = "",
    skip_peer: Callable[[Any], bool] | None = None,
    reason: str = "",
    follows: list[Follow] | None = None,
) -> int:
    """The `refines` verdict, after the new row is in: every non-pinned peer
    the new row replaces is superseded (status, superseded_by = the new row,
    invalid_at now — never deleted; recall drops it, the audit trail and `as_of`
    keep it). Best effort; returns the number of peers superseded. With `expected_sha256`
    (a verdict reached after the write) nothing is retired once the
    new row is another revision's, read sequentially right before the supersede;
    `skip_peer` leaves a peer whose head row it answers True for (the resolver's:
    revised since it was judged).

    The peers that did retire are recorded as `memory.auto_supersede`
    (`auto_resolution`): into `record_into` (reason -> entries) when the caller
    writes one record for many verdicts (a bulk write), else beside the response,
    under `reason` (default `newest_wins_contradiction`), the retired pairs added to
    `follows` for the caller to follow once its relations are in."""
    from server.core.ch import sequential_reads
    from server.memory.store import get_by_id, supersede_duplicates

    # A proposal (a fact on trial) retires nothing, whichever path reached this verdict.
    try:
        new_row = await get_by_id(
            new_memory_id, org=org, namespace=namespace, ch=ch, scope=WHOLE_NAMESPACE
        )
    except Exception:  # noqa: BLE001 — unreadable: the verdict applies
        new_row = None
    if new_row is not None and str(getattr(new_row, "status", "") or "") == "proposed":
        return 0
    victims = [
        {
            "memory_id": p.memory_id,
            "namespace": namespace,
            "agent": p.agent,
            "user_id": p.user_id,
            "session_id": p.session_id,
            "memory_key": p.memory_key,
            "superseded_by": new_memory_id,
        }
        for p in refined_peers
        if not p.pinned and p.memory_id and p.memory_id != new_memory_id
    ]
    if expected_sha256:
        # The verdict takes effect at this read: a revision committed after it is
        # the serial order "A refined its peers, then B replaced A", which leaves
        # the same rows. Fencing B itself would put a lock on every memory.set.
        # One difference: B's own inline check saw a peer active that this then
        # retires, so B's conflict_with can name a superseded row (review queue only).
        with sequential_reads():
            current = await get_by_id(
                new_memory_id, org=org, namespace=namespace, ch=ch, scope=WHOLE_NAMESPACE
            )
        if current is None or str(current.content_sha256 or "") != expected_sha256:
            return 0
    n = 0
    if victims:
        from server.memory.app.auto_resolution import (
            NEWEST_WINS_CONTRADICTION,
            RetirementTracker,
            owe_retirements,
            settle_retirements,
        )
        from server.memory.domain.lock import refuses_retirement

        tracker = RetirementTracker(
            lambda row: refuses_retirement(row) or (skip_peer is not None and skip_peer(row))
        )
        reason = reason or NEWEST_WINS_CONTRADICTION
        agent = str(getattr(new_row, "agent", "") or "")
        intent = ""
        if record_into is None:  # a caller recording into its own list owes its own intent
            planned = [{**v, "superseded_by_key": new_memory_key} for v in victims]
            intent = await owe_retirements(
                org=org, namespace=namespace, victims=planned, reason=reason, agent=agent
            )
        try:
            # Read-your-writes: a lagging replica's row would be rebuilt over a revision.
            n = await supersede_duplicates(
                org,
                victims,
                namespace=namespace,
                ch=ch or get_client(),
                skip=tracker.skip,
                strong=True,
                on_written=tracker.written,
                on_unconfirmed=tracker.unconfirmed,
                # A later verdict lands at the read + 1, below a write that started meanwhile.
                judged=bool(expected_sha256),
            )
        except Exception as e:  # noqa: BLE001 — the row is in; retirement is best effort
            logging.getLogger(__name__).warning("refines: supersede failed (non-fatal): %s", e)
        # What reached loom.memory, even when the embeddings mirror then failed.
        retired = [{**v, "superseded_by_key": new_memory_key} for v in tracker.retired(victims)]
        if record_into is not None:
            record_into.setdefault(reason, []).extend(retired)
        else:
            await settle_retirements(
                org=org,
                namespace=namespace,
                retired=retired,
                reason=reason,
                follows=[] if follows is None else follows,
                agent=agent,
                intent=intent,
            )
    return n


async def check_against_shared(
    *,
    org: str,
    namespace: str,
    target_key: str,
    memory_type: str,
    embedding: list[float],
) -> list[dict[str, Any]]:
    """Find scope='shared' rows in the same (org, namespace) that are
    semantically close to the value being promoted, but under a
    DIFFERENT memory_key. Returns peer rows (memory_key, value,
    distance) when cosine distance ≤ (1 - threshold); empty list when
    nothing conflicts.

    Same memory_key on re-promote is legitimate (supersedes via
    higher version) and is excluded — `WHERE memory_key != target_key`.

    Caller raises SHARED_CONFLICT when the list is non-empty.
    """
    if not embedding:
        # Embedding stub → skip rather than block promotion; worker reconciles.
        return []

    threshold = await _resolve_threshold(org, namespace, memory_type=memory_type)
    max_distance = 1.0 - threshold

    dim = len(embedding)  # equal-length filter; see check_on_commit

    ch = get_client()
    rows = await ch.query_all(
        """
        SELECT
            s.memory_key,
            s.value,
            """
        + "cosineDistance(e.embedding, {emb:Array(Float32)})"
        + """ AS dist
        FROM loom.shared s FINAL
        JOIN loom.memory_embeddings e FINAL
          ON  e.org = s.org AND e.namespace = s.namespace
          AND e.memory_key = s.memory_key
          AND e.scope = 'shared'
        WHERE s.org = {org:String}
          AND s.namespace = {ns:String}
          AND s.memory_type = {mt:String}
          AND s.memory_key != {self_key:String}
          AND s.tombstoned_at IS NULL
          AND e.tombstoned_at IS NULL
          AND e.embedding_dim = {dim:UInt16}
          AND length(e.embedding) > 0
        HAVING dist <= {max_dist:Float32}
        ORDER BY dist ASC
        LIMIT 5
        """,
        params={
            "org": org,
            "ns": namespace,
            "mt": memory_type,
            "self_key": target_key,
            "dim": dim,
            "emb": embedding,
            "max_dist": max_distance,
        },
    )
    return rows


# The rows a conflict mark may touch: the live ones, and a proposal on trial.
_MARKABLE = (*LIVE_STATUSES, "proposed")


async def mark_peers_conflicted(
    *,
    org: str,
    new_memory_id: str,
    peer_ids: list[str],
    expected_sha256: str = "",
    peer_sha256: dict[str, str] | None = None,
    namespace: str,
) -> str:
    """Cross-link the new row with its live peers in `namespace` and flag both
    `conflicted` (a proposal keeps its status). A row given a hash judged, the new row
    by `expected_sha256` or a peer by `peer_sha256`, is flagged only while it holds it,
    at its read version + 1. Returns
    `conflict` when flagged; `moved` when the new row was not (replaced, retired, or
    under `expected_sha256` no longer the judged write), and no peer is touched;
    `clear` when no peer was live."""
    import server.memory.store as memory_repo

    def _merged(existing: list[str], additions: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in [*existing, *additions]:
            val = str(item)
            if val and val not in seen:
                seen.add(val)
                out.append(val)
        return out

    # A strong read, so a retirement this write just made is seen: a peer retired
    # since the verdict stays retired. One retiring between this read and the new
    # row's write is still listed in the new row's `conflict_with` (the peer write
    # below skips it); closing that would need the new row's write confirmed, and
    # a mark is best effort.
    peers = await asyncio.gather(
        *(
            memory_repo.get_by_id(
                p, org=org, namespace=namespace, scope=WHOLE_NAMESPACE, fresh=True, strong=True
            )
            for p in peer_ids
        )
    )

    def _peer_moved(row: Any) -> bool:
        """A peer the judgement no longer covers: retired, or revised or pinned since."""
        if row.status not in _MARKABLE:
            return True
        if peer_sha256 is None:
            return False
        judged = peer_sha256.get(str(row.memory_id), "")
        return bool(row.pinned) or str(row.content_sha256 or "") != judged

    live = [
        p
        for p, row in zip(peer_ids, peers, strict=True)
        if row is not None and not _peer_moved(row)
    ]
    # Strong: the row was just written (or revised under its own id), and a stale
    # read would rebuild the version before it at a higher one. A row retired since
    # (by a verdict reached since, or the resolver's) is left retired, and so are its
    # peers.
    judged_skip, version = judged_write(expected_sha256)

    def _skip_new(row: Any) -> bool:
        return row.status not in _MARKABLE or (judged_skip is not None and judged_skip(row))

    if not live:
        if judged_skip is None:
            return "clear"  # an inline verdict: nothing left to flag
        # A verdict reached after the write says whether it would have flagged the row,
        # by the write's own rule: a revision since owes its own judgement, so this one
        # is moved.
        row = await memory_repo.get_by_id(
            new_memory_id,
            org=org,
            namespace=namespace,
            scope=WHOLE_NAMESPACE,
            fresh=True,
            strong=True,
        )
        return "clear" if row is not None and not _skip_new(row) else "moved"

    # A proposal (a fact on trial) keeps its status and only
    # records the contradiction; it is surfaced when the proposal is accepted
    # (memory.proposal_decide re-runs this for it). Until then an established
    # peer is not flagged over an untested row.
    new_is_proposal = False

    def _new(row: Any) -> dict[str, Any]:
        nonlocal new_is_proposal
        new_is_proposal = row.status == "proposed"
        return {
            "status": "proposed" if new_is_proposal else "conflicted",
            "conflict_with": _merged(row.conflict_with, live),
        }

    marked = await memory_repo.write_versions_by_ids(
        org,
        [new_memory_id],
        lambda row: _new(row) | version(row),
        namespace=namespace,
        skip=_skip_new,
        strong=True,
        judged=judged_skip is not None,
    )
    if not marked:
        return "moved"
    if new_is_proposal:
        return "conflict"
    # The peers are read again here, not built from `peers` above. Those rows carry
    # every column, but they were read before the new row was marked: a peer revised
    # or retired in the meantime would be written back over at a higher version,
    # undoing that change. Reading just before the write keeps that window small.
    await memory_repo.write_versions_by_ids(
        org,
        live,
        lambda row: {
            "status": "proposed" if row.status == "proposed" else "conflicted",
            "conflict_with": _merged(row.conflict_with, [new_memory_id]),
            # A judged peer lands above the row read, never above a later revision.
            **({"version": int(row.version) + 1} if peer_sha256 is not None else {}),
        },
        # Retired (or, judged, revised or pinned) between the read above and this
        # write: left as it is.
        namespace=namespace,
        skip=_peer_moved,
        strong=True,
        judged=peer_sha256 is not None,
    )
    return "conflict"


def judged_write(
    sha: str,
) -> tuple[Callable[[Any], bool] | None, Callable[[Any], dict[str, Any]]]:
    """(skip, version) for `write_versions_by_ids` landing a later verdict on the
    write it judged: a row of other content is left alone, and the new version is
    the read's + 1, so a revision committed after the read stays the newest row.
    No-ops without `sha` (an inline verdict, or a row stored without one)."""
    if not sha:
        return None, lambda _row: {}
    return (
        lambda row: str(getattr(row, "content_sha256", "") or "") != sha,
        lambda row: {"version": int(row.version) + 1},
    )


async def peer_rows(
    *,
    org: str,
    namespace: str,
    memory_type: str,
    memory_key: str,
    embedding: list[float],
    max_distance: float,
) -> list[dict[str, Any]]:
    """The rows `check_on_commit` would read for this write, fetched by the caller so it
    can pass them back (`candidate_rows`) and add what commits next: the writer's peers
    in its read scope and every pinned peer (a scope that does not resolve sees only
    pinned ones), with their distances."""
    ch = get_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "mt": memory_type,
        "self_key": memory_key,
        "emb": embedding,
        "max_dist": max_distance,
        "dim": len(embedding),
    }
    scope = _scope_or_none()
    rows = [] if scope is None else await nearest_peer_rows(ch, params, scope)
    return rows + await _foreign_pinned_rows(ch, params, scope, rows)


async def _without_erased(
    org: str, namespace: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The peers an erasure has not removed from the Postgres authority. ClickHouse still
    holds a peer until the erasure's ClickHouse steps run, and a verdict linking it is
    refused at commit (`pg_memory.PeerErased`)."""
    from server.core import pg
    from server.memory.store import pg_memory

    if not rows or not pg.enabled():
        return rows
    gone = await pg_memory.erased_ids(org, namespace, [str(r["memory_id"]) for r in rows])
    return [r for r in rows if str(r["memory_id"]) not in gone]


def trim_peers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nearest first, the shape `peer_rows` returns: at most `PEER_LIMIT` rows inside the
    writer's read scope, and the pinned rows outside it among the `PEER_LIMIT` nearest
    pinned ones (`PINNED_PEERS_SQL`)."""
    scope = _scope_or_none()

    def inside(r: Mapping[str, Any]) -> bool:
        return scope is not None and row_in_read_scope(scope, r.get("user_id"), r.get("session_id"))

    rows = sorted(rows, key=lambda r: (float(r["dist"]), str(r.get("memory_key") or "")))
    near = [r for r in rows if inside(r)][:PEER_LIMIT]
    pinned = [r for r in rows if r.get("pinned")][:PEER_LIMIT]
    return near + [r for r in pinned if not inside(r)]


def could_be_peer(row: Mapping[str, Any]) -> bool:
    """Whether a committed row is a peer `peer_rows` would return for this writer, its
    type, key and distance aside: live, active or conflicted, and inside the writer's
    read scope or pinned."""
    if row.get("tombstoned_at") is not None:
        return False
    if str(row.get("status") or "active") not in ("active", "conflicted"):
        return False
    scope = _scope_or_none()
    if row.get("pinned"):
        return True
    return scope is not None and row_in_read_scope(scope, row.get("user_id"), row.get("session_id"))


async def peer_distance(org: str, namespace: str, memory_type: str, cfg: Any) -> float:
    """How far `check_on_commit`'s peer statements reach for this write: a commit
    in that radius can change the verdict (`pg_admission.absorb`)."""
    threshold = cfg.threshold
    if threshold is None:
        threshold = await _resolve_threshold(org, namespace, memory_type=memory_type)
    return _peer_query_distance(float(threshold))
