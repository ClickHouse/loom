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
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

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
    PendingJudge,
    WriteConfig,
    _as_datetime,
    _fold_judged,
    canonical,
    merge_or_run_duplicate,
    query_max_distance,
    same_content_words,
    templated_substitution,
)
from server.memory.store import conflicts as _conflicts_repo
from server.memory.store.conflict_candidates import (
    _PEER_ROWS_SQL,
    PINNED_PEERS_SQL,
    _peer_candidates_sql,
    candidate_sql_many,
)
from server.settings import settings

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


async def resolve_conflict_floor(org: str, namespace: str, memory_type: str) -> tuple[float, float]:
    """(threshold, max_distance) for one namespace + memory_type — the two
    numbers `check_on_commit` resolves before its statement, exposed so a bulk
    writer resolves them once per type instead of once per fact."""
    threshold = await _resolve_threshold(org, namespace, memory_type=memory_type)
    return threshold, query_max_distance(threshold)


async def candidate_rows_many(
    *,
    org: str,
    namespace: str,
    items: list[tuple[str, str, list[float], float]],
    batch: int | None = None,
    ch: Any = None,
) -> dict[int, list[dict[str, Any]]]:
    """Peer rows for many facts at once, keyed by the fact's index in `items`
    (each `(memory_type, memory_key, embedding, max_distance)`). Facts are
    grouped by embedding dimension, cut into chunks of `batch`
    (`memory_write_candidate_batch`), and the chunks run concurrently — the
    same concurrency as the per-fact path, at one namespace read per chunk
    instead of per fact. A fact with no embedding gets no entry. Fail-open per
    chunk: a failed chunk's facts fall back to the per-fact statement in the
    caller by getting no entry here — callers treat a missing key as "fetch
    yourself", not as "no peers"."""
    import asyncio

    ch = ch or get_client()
    scope = _scope_or_none()
    if scope is None:
        return {}  # every fact falls back to the per-fact path, which fails closed
    size = int(batch if batch is not None else settings.memory_write_candidate_batch)
    size = max(size, 1)
    by_dim: dict[int, list[int]] = {}
    for i, (_mt, _k, emb, _md) in enumerate(items):
        if emb:
            by_dim.setdefault(len(emb), []).append(i)
    out: dict[int, list[dict[str, Any]]] = {}

    async def _chunk(dim: int, idxs: list[int]) -> None:
        params: dict[str, Any] = {
            "org": org,
            "ns": namespace,
            "dim": dim,
            "embs": [items[i][2] for i in idxs],
            "types": [items[i][0] for i in idxs],
            "keys": [items[i][1] for i in idxs],
            "max_dists": [float(items[i][3]) for i in idxs],
        }
        in_scope = scope_and(scope, params, "m")
        rows = await ch.query_all(candidate_sql_many(len(idxs), in_scope), params=params)
        for i in idxs:
            out[i] = []
        for r in rows:
            q = int(r.get("qidx") or 0)
            if 0 <= q < len(idxs):
                out[idxs[q]].append(dict(r))

    jobs = [
        _chunk(dim, idxs[k : k + size])
        for dim, idxs in by_dim.items()
        for k in range(0, len(idxs), size)
    ]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for res in results:
        if isinstance(res, Exception):
            log.warning("batched conflict candidates failed (per-fact fallback): %s", res)
    return out


async def _policy_before_gate(
    resolved_policy: str | None, candidate_rows: Any, org: str, namespace: str
) -> str | None:
    """The namespace policy the gate consults before its peer
    statement. Under `append_only` the namespace's writes are events, not
    claims — a run's report is not a restatement of last run's because it
    shares the template — so every write is its own row: no merge, duplicate
    or conflict verdict against a peer (a same-key rewrite stays
    last-writer-wins by version) and the peer statement is never issued.
    The prefetched policy when the caller had it; else one point read on the
    per-fact path (a bulk caller that prefetched its candidate rows passes
    the policy with them and pays no read here); an unreadable policy keeps
    the cosine path (None)."""
    if resolved_policy is not None or candidate_rows is not None:
        return resolved_policy
    try:
        return await namespace_conflict_policy(org, namespace, ch=get_client())
    except Exception as e:  # noqa: BLE001 — unknown policy keeps the gate
        logging.getLogger(__name__).warning(
            "conflict gate: policy read failed (%s); the gate runs", e
        )
        return None


async def _peer_rows(
    ch: Any, params: dict[str, Any], scope: ReadFilters = WHOLE_NAMESPACE
) -> list[dict[str, Any]]:
    """The gate's peers: this fact's cosine neighbours under the same type,
    excluding its own key (a re-write is LWW by version, not a conflict), at
    most `_PEER_LIMIT`, nearest first.

    Two statements instead of the `memory FINAL JOIN memory_embeddings FINAL` scan.
    Stage A ranks the namespace's vectors alone, in the shape the `Quantized` codec
    answers codes-first (a bare distance ORDER BY + LIMIT and
    `vector_search_settings()`), guarded on `embedding.size0` — `length(embedding)`
    decodes the full-precision column and turns codes-first off (read volume roughly 4x
    higher). Stage B reads the candidates' `loom.memory` rows by sort key and applies
    the row-side filters (type, status, tombstone), which is where the two tables can
    disagree (vectors of non-live rows, live rows whose vector says otherwise), so stage
    A filters nothing the join did not filter on the vector side. When fewer than
    `_PEER_LIMIT` survive while stage A's last candidate is still inside `max_dist`,
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
        if len(out) >= _PEER_LIMIT or exhausted:
            break
        if k >= _PEER_MAX_K:
            return await _peer_rows_joined(ch, params, scope_and(scope, params, "m"))
        k *= 4
    out.sort(key=lambda r: (r["dist"], str(r.get("memory_key") or "")))
    return out[:_PEER_LIMIT]


_PEER_LIMIT = 10
_PEER_FIRST_K = 32
_PEER_MAX_K = 2048


def _ident(r: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(r.get("agent") or ""),
        str(r.get("user_id") or ""),
        str(r.get("session_id") or ""),
        str(r.get("memory_key") or ""),
    )


async def _peer_rows_joined(
    ch: Any, params: dict[str, Any], in_scope: str = ""
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
        + """
        HAVING dist <= {max_dist:Float32}
        ORDER BY dist ASC
        LIMIT 10
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


async def _foreign_pinned_rows(
    ch: Any, params: dict[str, Any], scope: ReadFilters | None, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The pinned peers the scoped peer query cannot see, for a writer bounded
    below the namespace: a pinned row of another user still blocks its write."""
    if scope is not None and not scope.narrowed:
        return []
    seen = {str(r.get("memory_id")) for r in rows}
    pinned = await ch.query_all(PINNED_PEERS_SQL, params=params)
    return [dict(r) for r in pinned if str(r.get("memory_id")) not in seen]


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
    from server.memory.app.conflict_judge import Judgement

    judged = Judgement(
        verdict="conflict",
        ran=True,
        keep=[p.memory_id for p in subst],
        labels={p.memory_id: "contradiction" for p in subst},
    )
    return _fold_judged(
        judged,
        peers,
        cosine_peers=peers,
        policy=policy,
        new_valid_at=new_valid_at,
        new_value=new_value,
    )


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
    defer_judge: bool = False,
) -> ConflictResult:
    """Run the 4-step algorithm. Caller raises 409 on verdict=='rejected'
        and applies merge/conflict flags on insert.

        `resolved_duplicate_threshold` / `resolved_policy` / `allowlisted_keys`
    : the namespace reads a point write prefetched in one round
        trip (`prefetch_write_config`); None reads them here.
        `defer_judge=True` returns `pending` with a `PendingJudge` where the
        inline path would have called the LLM; every deterministic tier (merge,
        rejected, templated substitution, duplicate) still answers inline.

        `new_run_id`: a write made under a run keeps its own row.
        A canonical-form match with a peer from another run, or from no run,
        is returned as `duplicate` (insert, status='duplicate', `duplicate_of`
        the peer) rather than `merge` (no insert), so the run's `run_id`-scoped
        reads find what the run wrote. Same-run restatements still merge.

        `new_valid_at`: the new row's validity date when the writer supplied one; it decides
        which side of a judged contradiction `newest_wins` retires.

        `candidate_rows` / `resolved_threshold`: a bulk writer that already fetched this
        fact's peers with `candidate_rows_many` and resolved the namespace threshold passes
        them in; the statement and the two threshold lookups are then skipped and every step
        below runs on the same rows the per-fact statement would have returned."""
    # Non-searchable types (episodic) carry no embedding → skip cleanly.
    if not new_embedding:
        return ConflictResult(verdict="clear", peers=[])

    resolved_policy = await _policy_before_gate(resolved_policy, candidate_rows, org, namespace)
    if resolved_policy == APPEND_ONLY_POLICY:  # no verdict against a peer
        return ConflictResult(verdict="clear", peers=[])

    threshold = (
        float(resolved_threshold)
        if resolved_threshold is not None
        else await _resolve_threshold(org, namespace, memory_type=memory_type)
    )
    # With the judge on, the peer query reaches down to a lower floor. Peers in the band
    # below `threshold` are candidates for the judge only — they never produce a cosine
    # verdict on their own — so recall widens exactly where the judge's precision pays
    # for it (updates that share only ~20% of their words and sit under different keys
    # are never seen by the 0.85 flag).
    max_distance = query_max_distance(threshold)

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
        rows = await _peer_rows(ch, params, scope)
    rows += await _foreign_pinned_rows(ch, params, scope, rows)
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

    new_canonical = canonical(new_value)
    all_peers, foreign = _split_by_read_scope([_peer_of(r) for r in rows])
    # Flagged peers reach the cosine verdicts below; band peers only the judge.
    peers = [p for p in all_peers if p.similarity >= threshold]
    band_peers = [p for p in all_peers if p.similarity < threshold]
    # A pinned row outside the writer's read scope still blocks it.
    foreign_blocker = next((p for p in foreign if p.pinned and p.similarity >= threshold), None)
    if not peers and foreign_blocker is None:
        pj = PendingJudge(
            org,
            namespace,
            new_value,
            band_peers,
            [],
            "clear",
            new_valid_at,
            new_run_id,
            resolved_policy,
        )
        return await _judge_or_defer(pj, defer_judge)

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
    # Deterministic; the judge is not asked (it called "created in the country
    # of Soviet Union" a restatement of "… United States of America").
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
    # falls through to the judge, whose contradiction verdict `newest_wins`
    # can act on.
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
    # above conflict threshold) → conflict, unless the judge
    # says the pair is an elaboration, a restatement or unrelated (most cosine
    # "conflicts" are not).
    pj = PendingJudge(
        org,
        namespace,
        new_value,
        peers + band_peers,
        peers,
        "conflict",
        new_valid_at,
        new_run_id,
        resolved_policy,
    )
    return await _judge_or_defer(pj, defer_judge)


async def namespace_conflict_policy(org: str, namespace: str, *, ch: Any) -> str:
    """The namespace's `default_conflict_policy`, or the default when unset."""
    return _policy_from(await _conflicts_repo.namespace_write_row(org, namespace, ch=ch))


async def _policy_for(org: str, namespace: str, judged: Any) -> str:
    """The namespace conflict policy, read only when a judged contradiction
    could fold to supersession under it. '' on any error."""
    if getattr(judged, "verdict", "") != "conflict":
        return ""
    try:
        return await namespace_conflict_policy(org, namespace, ch=get_client())
    except Exception as e:  # noqa: BLE001 — unknown policy keeps the flag
        logging.getLogger(__name__).warning("conflict judge: policy read failed (%s)", e)
        return ""


async def _judge_conflict(new_value: str, peers: list[ConflictPeer]) -> Any:
    """One cheap call labels the closest peers. None when off."""
    if not bool(settings.memory_conflict_judge):
        return None
    from server.memory.app import conflict_judge

    cap = max(int(settings.memory_conflict_judge_max_peers), 1)
    try:
        from server.llm.client import get_llm_client, get_llm_client_for_model

        cheap = settings.memory_ask_synth_model_cheap.strip()
        llm = get_llm_client_for_model(cheap) if cheap else get_llm_client()
    except Exception as e:  # noqa: BLE001 — no client, cosine verdict stands
        logging.getLogger(__name__).warning("conflict judge: no LLM client (%s)", e)
        return None
    # peers arrive ordered by distance ascending; judge the closest few.
    return await conflict_judge.judge(llm, new_value, peers[:cap])


def judge_enabled() -> bool:
    return bool(settings.memory_conflict_judge)


async def _judge_or_defer(pj: PendingJudge, defer: bool) -> ConflictResult:
    """With `defer` the write gets `pending` and the judge's
    input; otherwise the judge runs now. The judge switched off never
    defers — there is nothing to wait for and the cosine verdict is final."""
    if defer and judge_enabled() and pj.candidates:
        return ConflictResult(verdict="pending", peers=list(pj.candidates), pending_judge=pj)
    return await settle_deferred_judgement(pj)


async def settle_deferred_judgement(pj: PendingJudge) -> ConflictResult:
    """Run the judge over a `PendingJudge` and fold its labels exactly as the
    inline path does: no judge or an unparsable answer → the cosine fallback
    verdict over the cosine peers; else `_fold_judged` over the candidates
    with the flagged peers as the conflict fallback and the namespace policy
    (prefetched or read now). The inline path calls this too, so the two
    cannot drift."""
    judged = await _judge_conflict(pj.new_value, pj.candidates)
    if judged is None or not judged.ran:
        return ConflictResult(verdict=pj.fallback_verdict, peers=list(pj.cosine_peers))
    if pj.policy is None:
        policy = await _policy_for(pj.org, pj.namespace, judged)
    else:  # prefetched; read only for a judged contradiction
        policy = pj.policy if judged.verdict == "conflict" else ""
    return _fold_judged(
        judged,
        pj.candidates,
        cosine_peers=pj.cosine_peers or None,
        policy=policy,
        new_valid_at=pj.new_valid_at,
        new_run_id=pj.new_run_id,
        new_value=pj.new_value,
    )


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


def truncating_refinement(new_value: str, peer_value: str) -> bool:
    """Whether `new_value` drops a measured token `peer_value` carries. Pure."""
    from server.memory.app.value_updates import measured_tokens

    if not peer_value:
        return False
    return bool(measured_tokens(peer_value) - measured_tokens(new_value or ""))


async def _truncating_refines(
    org: str,
    namespace: str,
    new_memory_id: str,
    new_value: str,
    refined_peers: list[ConflictPeer],
    *,
    ch: Any = None,
) -> set[str]:
    """The peers a refines verdict must NOT retire (see `apply_refines`).
    Reads the new row's text when the caller had none; fail-open to none."""
    if not refined_peers:
        return set()
    text = new_value or ""
    if not text:
        try:
            rows = await (ch or get_client()).query_all(
                "SELECT toString(value) AS v FROM loom.memory FINAL WHERE org = {org:String} "
                "AND namespace = {ns:String} AND toString(memory_id) = {mid:String} LIMIT 1",
                params={"org": org, "ns": namespace, "mid": str(new_memory_id)},
            )
            text = str(rows[0]["v"]) if rows else ""
        except Exception:  # noqa: BLE001 — no text, no guard
            return set()
    if not text:
        return set()
    return {p.memory_id for p in refined_peers if truncating_refinement(text, p.value or "")}


async def apply_refines(
    *,
    org: str,
    namespace: str,
    new_memory_id: str,
    new_memory_key: str,
    refined_peers: list[ConflictPeer],
    ch: Any = None,
    run_id: str = "-",
    relation: str = "refines",
    new_value: str = "",
) -> int:
    """The `refines` verdict, after the new row is in: every
    non-pinned refined peer is superseded (status, superseded_by = the new
    row, invalid_at now — never deleted; recall drops it, the audit trail and
    `as_of` keep it) and a `refines` edge from the new key to the old records
    why. Best effort; returns the number of peers superseded.

    A peer whose text carries a measured token the new row lacks (a duration, a count, a
    price, a date, a month) keeps its row — the new statement elaborates a facet but
    truncates the record, and the question that needs the measure ("how long did it
    take") would read "not recorded". The edge is still written. `new_value` is the new
    row's text; when the caller has none it is read from the store."""
    from server.memory.store import get_by_id, supersede_duplicates

    # A proposal (a fact on trial) retires nothing — whichever
    # path reached this verdict (inline, deferred settle, the sweep, bulk).
    try:
        new_row = await get_by_id(new_memory_id, org=org, ch=ch, scope=WHOLE_NAMESPACE)
    except Exception:  # noqa: BLE001 — unreadable: the verdict applies
        new_row = None
    if new_row is not None and str(getattr(new_row, "status", "") or "") == "proposed":
        return 0
    keep = await _truncating_refines(org, namespace, new_memory_id, new_value, refined_peers, ch=ch)
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
        if not p.pinned and p.memory_id and p.memory_id != new_memory_id and p.memory_id not in keep
    ]
    n = 0
    if victims:
        try:
            from server.memory.domain.lock import refuses_retirement

            n = await supersede_duplicates(
                org, victims, ch=ch or get_client(), skip=refuses_retirement
            )
        except Exception as e:  # noqa: BLE001 — the row is in; retirement is best effort
            logging.getLogger(__name__).warning("refines: supersede failed (non-fatal): %s", e)
    for p in refined_peers:
        if p.memory_key == new_memory_key:
            continue
        try:
            from server.memory.api.models_graph import MemoryLinkInput
            from server.memory.app.ops import handle_memory_link

            await handle_memory_link(
                MemoryLinkInput(
                    org=org,
                    namespace=namespace,
                    from_key=new_memory_key,
                    to_key=p.memory_key,
                    relation=relation,
                    explanation=(
                        "Judged on write: the newer memory restates this "
                        "one with more detail; the older row is superseded, not deleted."
                        if relation == "refines"
                        else "Judged on write: the newer memory contradicts "
                        "this one and the namespace is newest_wins; the older row is "
                        "superseded, not deleted."
                    ),
                    confidence=0.9,
                    run_id=run_id or "-",
                )
            )
        except Exception as e:  # noqa: BLE001 — the refines edge is best effort; the row is in
            logging.getLogger(__name__).warning("refines: edge write failed (non-fatal): %s", e)
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


async def mark_peers_conflicted(
    *,
    org: str,
    new_memory_id: str,
    peer_ids: list[str],
) -> None:
    """After a conflicting insert, populate conflict_with on the new
    row AND on each existing peer, and flip all of them to
    status='conflicted'. Writes new RMT version rows so every persisted
    column is preserved by memory_repo's mirror helper.
    """
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

    # A proposal (a fact on trial) keeps its status and only
    # records the contradiction; it is surfaced when the proposal is accepted
    # (memory.proposal_decide re-runs this for it). Until then an established
    # peer is not flagged over an untested row.
    new_is_proposal = False

    def _new(row: Any) -> dict[str, Any]:
        nonlocal new_is_proposal
        new_is_proposal = str(getattr(row, "status", "") or "") == "proposed"
        return {
            "status": "proposed" if new_is_proposal else "conflicted",
            "conflict_with": _merged(row.conflict_with, peer_ids),
        }

    await memory_repo.write_versions_by_ids(org, [new_memory_id], _new)
    if new_is_proposal:
        return
    await memory_repo.write_versions_by_ids(
        org,
        peer_ids,
        lambda row: {
            "status": "proposed"
            if str(getattr(row, "status", "") or "") == "proposed"
            else "conflicted",
            "conflict_with": _merged(row.conflict_with, [new_memory_id]),
        },
    )
