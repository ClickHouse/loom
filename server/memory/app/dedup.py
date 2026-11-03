"""Memory dedup + lineage handlers.

Read-side analytics + admin surfaces on top of the core memory plane.
Only `memory.dedup_resolve` mutates `loom.memory` (targeted tombstones via
repo helpers); kept separate from `memory/app/handlers.py` so that stays CRUD.

Wire ops:
  - memory.lineage       — promoted-memory provenance walk
  - memory.related       — entity-overlap recall
  - memory.dedup_list    — operator triage of status='duplicate' rows
  - memory.dedup_resolve — resolve a duplicate pair
  - memory.dedup_collapse — every pair of a namespace at once, no LLM,
                            nothing tombstoned
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_graph import (
    MemoryDedupCollapseInput,
    MemoryDedupCollapseOutput,
    MemoryDedupListInput,
    MemoryDedupListOutput,
    MemoryDedupResolveInput,
    MemoryDedupResolveOutput,
    MemoryLineageInput,
    MemoryLineageOutput,
    MemoryRelatedInput,
    MemoryRelatedOutput,
    _DedupCluster,
    _DedupCollapsePair,
    _DedupDuplicate,
    _LineageAuditEvent,
    _LineageCandidate,
    _LineageSupporter,
    _MemoryRelatedHit,
)
from server.memory.app import webhooks
from server.memory.domain import assistant_rules
from server.memory.store.rows import MemoryRow


def _parse_supporter(tuple_str: str) -> dict[str, str] | None:
    """Parse one entry of `loom.shared.promoted_from`.

    Wire format: `org/namespace/agent/user_id/session_id/memory_key`.
    Returns None on mismatch so one malformed row doesn't 500 the lineage.
    """
    parts = tuple_str.split("/")
    if len(parts) != 6:
        return None
    return {
        "org": parts[0],
        "namespace": parts[1],
        "agent": parts[2],
        "user_id": parts[3],
        "session_id": parts[4],
        "memory_key": parts[5],
    }


async def _lineage_from_memory_tags(input: MemoryLineageInput) -> MemoryLineageOutput:
    """Resolve provenance for a memory that lives in loom.memory only
    (no loom.shared row). Seeder candidates and curated-doc imports write
    a `candidate-id:<uuid>` or `source-kind:<kind>` tag we read back.
    """
    from server.core.ch import get_client

    ch = get_client()
    row = await ch.query_one(
        """
        SELECT tags, created_at, user_id, agent, actor, source_trace, source_resource_id
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND memory_key = {k:String}
          AND tombstoned_at IS NULL
        LIMIT 1
        """,
        params={"org": input.org, "ns": input.namespace, "k": input.memory_key},
    )
    if row is None:
        return MemoryLineageOutput(found=False, memory_key=input.memory_key)

    tags = list(row.get("tags") or [])
    cand_id = ""
    source_kind = ""
    for t in tags:
        if t.startswith("candidate-id:"):
            cand_id = t[len("candidate-id:") :]
        elif t.startswith("source-kind:"):
            source_kind = t[len("source-kind:") :]

    candidate = None
    # Seeder and supporters rows: the approver is the row's user_id.
    promoted_by = str(row.get("user_id") or "")
    if cand_id:
        from server.memory.store import learnings as learnings_repo

        cand_row = await learnings_repo.find_candidate(
            input.org,
            input.namespace,
            cand_id,
        )
        if cand_row is not None:
            supporter_count = len(cand_row.get("supporters") or [])
            if cand_row.get("source_kind") == assistant_rules.SOURCE_KIND:
                # Its support lives in loom.learnings_supporters: distinct users.
                stats = await learnings_repo.supporter_stats(input.org, input.namespace, [cand_id])
                supporter_count = int(stats[0]["users"]) if stats else 0
                # A bot's rule is written at user_id '-'; its approver is the row's actor.
                promoted_by = str(row.get("actor") or "") or promoted_by
            candidate = _LineageCandidate(
                candidate_id=cand_id,
                source_kind=cand_row.get("source_kind", "") or source_kind,
                supporter_count=supporter_count,
                rationale=cand_row.get("rationale", "") or "",
                decided_at=cand_row.get("decided_at") or row["created_at"],
                decided_by=cand_row.get("decided_by", "") or "",
                match_confidence="exact",
                seeded_from_thread_id=(cand_row.get("seeded_from_thread_id") or ""),
                seeded_from_msg_id=(cand_row.get("seeded_from_msg_id") or ""),
            )

    return MemoryLineageOutput(
        found=True,
        memory_key=input.memory_key,
        # No loom.shared promotion_event for memory-only rows.
        promotion_event="",
        promoted_by=promoted_by,
        promoted_at=row.get("created_at"),
        rationale=f"Imported via {source_kind}" if source_kind else "",
        source_trace=str(row.get("source_trace") or ""),
        source_resource_id=str(row.get("source_resource_id") or ""),
        mechanism=(
            "review" if cand_id else ("import" if source_kind == "curated_doc" else "unknown")
        ),
        supporters=[],
        candidate=candidate,
        audit_chain=[],
    )


async def handle_memory_lineage(input: MemoryLineageInput) -> MemoryLineageOutput:
    """Assemble the provenance chain for a shared memory.

    Fans out the target shared row, its audit chain (by promotion_event),
    and a best-effort candidate match into one "how did this get here?"
    response.
    """
    from server.insights.app.audit import audit_rows_for_request
    from server.memory.store import shared as shared_repo
    from server.observability.otel import traced

    with traced(
        "ch.query.memory.lineage",
        memory_key=input.memory_key,
        scope=input.scope,
    ):
        target = await shared_repo.fetch_shared(
            input.org,
            input.namespace,
            input.memory_key,
        )
        if target is None or target.get("tombstoned_at") is not None:
            # Memories promoted via memory.set live in loom.memory only;
            # resolve their lineage from the candidate-id tag instead.
            return await _lineage_from_memory_tags(input)

        promotion_event = str(target.get("promotion_event") or "")
        promoted_by = target.get("promoted_by") or ""
        promoted_at: Any = target.get("promoted_at")
        rationale = target.get("rationale") or ""

        # auto-promote worker writes 'auto'; learnings.review writes the
        # approver email.
        mechanism: Literal["auto", "review", "unknown"]
        if promoted_by == "auto":
            mechanism = "auto"
        elif promoted_by:
            mechanism = "review"
        else:
            mechanism = "unknown"

        audit_rows = (
            await audit_rows_for_request(promotion_event, limit=100) if promotion_event else []
        )

        # Candidate match: exact via promoted_from_candidate_id, else a
        # time-window fallback (match_confidence='inferred'). Auto-promote
        # rows skip both — the worker bypasses learnings_queue.
        candidate = None
        if mechanism == "review":
            cand_id_raw = target.get("promoted_from_candidate_id")
            cand_id = str(cand_id_raw) if cand_id_raw else ""
            if cand_id:
                from server.memory.store import learnings as learnings_repo

                cand_row = await learnings_repo.find_candidate(
                    input.org,
                    input.namespace,
                    cand_id,
                )
                if cand_row is not None:
                    candidate = _LineageCandidate(
                        candidate_id=cand_id,
                        source_kind=cand_row.get("source_kind", "") or "",
                        supporter_count=len(
                            cand_row.get("supporters") or [],
                        ),
                        rationale=cand_row.get("rationale", "") or "",
                        decided_at=cand_row.get("decided_at") or promoted_at,
                        decided_by=cand_row.get("decided_by", "") or "",
                        match_confidence="exact",
                        seeded_from_thread_id=(cand_row.get("seeded_from_thread_id") or ""),
                        seeded_from_msg_id=(cand_row.get("seeded_from_msg_id") or ""),
                    )
            elif promoted_at is not None:
                cand_row = await shared_repo.find_candidate_for_promotion(
                    input.org,
                    input.namespace,
                    input.memory_key,
                    promoted_at,
                    input.candidate_match_window_s,
                )
                if cand_row is not None:
                    candidate = _LineageCandidate(
                        candidate_id=str(cand_row["candidate_id"]),
                        source_kind=cand_row.get("source_kind", "") or "",
                        supporter_count=int(cand_row.get("supporter_count") or 0),
                        rationale=cand_row.get("rationale", "") or "",
                        decided_at=cand_row["decided_at"],
                        decided_by=cand_row.get("decided_by", "") or "",
                        match_confidence="inferred",
                        seeded_from_thread_id=(cand_row.get("seeded_from_thread_id") or ""),
                        seeded_from_msg_id=(cand_row.get("seeded_from_msg_id") or ""),
                    )

    supporters: list[_LineageSupporter] = []
    for s in target.get("promoted_from") or []:
        parsed = _parse_supporter(str(s))
        if parsed is not None:
            supporters.append(_LineageSupporter(**parsed))

    audit_chain = [
        _LineageAuditEvent(
            ts=r.ts,
            event_type=r.event_type,
            actor=r.actor,
            request_id=r.request_id,
        )
        for r in audit_rows
    ]

    return MemoryLineageOutput(
        found=True,
        memory_key=input.memory_key,
        promotion_event=promotion_event,
        promoted_by=promoted_by,
        promoted_at=promoted_at,
        rationale=rationale,
        mechanism=mechanism,
        supporters=supporters,
        candidate=candidate,
        audit_chain=audit_chain,
    )


async def handle_memory_dedup_list(input: MemoryDedupListInput) -> MemoryDedupListOutput:
    """List status='duplicate' clusters under a namespace.

    Scans `find_duplicate_clusters`, then point-fetches each canonical row
    to enrich the response with memory_key + agent for display.
    """
    import server.memory.store as memory_repo

    rows = await memory_repo.find_duplicate_clusters(
        input.org,
        input.namespace,
        limit=input.limit,
    )

    clusters: list[_DedupCluster] = []
    for row in rows:
        canonical_id = row.canonical_id
        canonical_key = ""
        canonical_agent = ""
        if canonical_id:
            canon = await memory_repo.get_canonical_row(input.org, canonical_id)
            if canon is not None:
                canonical_key = canon.memory_key
                canonical_agent = canon.agent
        duplicates = [
            _DedupDuplicate(
                memory_id=str(d[0]),
                memory_key=d[1],
                agent=d[2],
                created_at=d[3],
            )
            for d in row.dups
        ]
        clusters.append(
            _DedupCluster(
                canonical_id=canonical_id,
                canonical_key=canonical_key,
                canonical_agent=canonical_agent,
                dup_count=row.dup_count,
                duplicates=duplicates,
            )
        )

    return MemoryDedupListOutput(clusters=clusters)


async def handle_memory_dedup_resolve(input: MemoryDedupResolveInput) -> MemoryDedupResolveOutput:
    """Resolve one duplicate pair, addressed by the two memory ids.

    Three decisions dispatch to:
      keep_canonical → tombstone the duplicate row
      keep_duplicate → tombstone canonical + clear duplicate's tag
      keep_both      → clear duplicate's tag only (both stay live)

    A key alone does not name a row: several identities can hold the same
    key, so the pair is the duplicate id plus the canonical id it points at.
    `confirm_canonical_key` must equal the canonical row's key.
    """
    from fastapi import HTTPException

    import server.memory.store as memory_repo
    from server.memory.domain import lock as _lock

    async def _live_row(memory_id: str) -> MemoryRow | None:
        try:
            # The dashed form: ClickHouse refuses the braced and urn: forms Python reads.
            memory_id = str(uuid.UUID(memory_id))
        except ValueError:
            return None  # not a UUID: no such row
        row = await memory_repo.get_by_id(
            memory_id, org=input.org, fresh=True, strong=True, scope=WHOLE_NAMESPACE
        )
        return row if row is not None and row.namespace == input.namespace else None

    dup_row = await _live_row(input.duplicate_id)
    canon_row = await _live_row(input.canonical_id)
    if (
        dup_row is None
        or canon_row is None
        or dup_row.status != "duplicate"
        or dup_row.duplicate_of != canon_row.memory_id
    ):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "DEDUP_PAIR_NOT_FOUND",
                "message": (
                    "No live row in this namespace is `duplicate_id` marked as a "
                    "duplicate of `canonical_id`. Take both ids from memory.dedup_list."
                ),
            },
        )
    if input.confirm_canonical_key != canon_row.memory_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONFIRM_CANONICAL_KEY_MISMATCH",
                "message": (
                    "`confirm_canonical_key` must equal the canonical row's key. "
                    "Mid-weight defense against destructive UI-dropdown "
                    "accidents."
                ),
            },
        )

    canonical_tombstoned = False
    duplicate_tombstoned = False

    async def _tombstone(row: MemoryRow) -> None:
        # A locked side of the pair cannot be the one tombstoned.
        _lock.assert_unlocked(row, "delete", op="memory.dedup_resolve")
        tombstoned = await memory_repo.tombstone_by_id(input.org, row.memory_id)
        await webhooks.announce([("memory.tombstoned", change) for change in tombstoned])

    if input.decision == "keep_canonical":
        await _tombstone(dup_row)
        duplicate_tombstoned = True
    elif input.decision == "keep_duplicate":
        await _tombstone(canon_row)
        await memory_repo.clear_duplicate_tag(input.org, dup_row.memory_id)
        canonical_tombstoned = True
    elif input.decision == "keep_both":
        await memory_repo.clear_duplicate_tag(input.org, dup_row.memory_id)

    return MemoryDedupResolveOutput(
        canonical_key=canon_row.memory_key,
        duplicate_key=dup_row.memory_key,
        decision=input.decision,
        canonical_tombstoned=canonical_tombstoned,
        duplicate_tombstoned=duplicate_tombstoned,
    )


async def handle_memory_related(input: MemoryRelatedInput) -> MemoryRelatedOutput:
    """Return memories that share entities with the source memory.

    Indirect-connection recall via `arrayIntersect` over the `entities`
    column on `loom.memory`, rather than a real graph store.

    Requires the entity_extractor worker enabled
    (`LOOM_ENTITY_EXTRACT_ENABLED=true`). When the source row has
    `entities = []` (extractor off or hasn't processed it yet) we
    return `results=[]` with `reason="source_has_no_entities"` so
    the caller can distinguish "no matches" from "extractor disabled".
    """
    import server.memory.store as memory_repo
    from server.observability.otel import traced

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    with traced("ch.query.memory.related", memory_key=input.memory_key):
        source_entities, rows = await memory_repo.find_related_by_entities(
            identity,
            input.memory_key,
            top_k=input.top_k,
            min_overlap=input.min_overlap,
            scope=request_scope(),
        )

    if not source_entities:
        # Distinguish "row missing" from "row exists but no entities".
        src = await memory_repo.get_by_key(
            identity, input.memory_key, fresh=True, scope=request_scope()
        )
        reason = "source_not_found" if src is None else "source_has_no_entities"
        return MemoryRelatedOutput(results=[], source_entities=[], reason=reason)

    hits = [
        _MemoryRelatedHit(
            memory_key=r["memory_key"],
            memory_type=r["memory_type"],
            content_excerpt=r["content_excerpt"],
            overlap_count=int(r["overlap_count"]),
            shared_entities=list(r["shared_entities"]),
        )
        for r in rows
    ]
    return MemoryRelatedOutput(
        results=hits,
        source_entities=source_entities,
        reason="",
    )


async def handle_memory_dedup_collapse(
    input: MemoryDedupCollapseInput,
) -> MemoryDedupCollapseOutput:
    """Collapse every duplicate pair of a namespace onto its canonical without an LLM,
    keeping every key and tombstoning nothing.

    A `status='duplicate'` row already defers to `duplicate_of` in `loom.memory`: the read
    path hides it there and serves the canonical. What this pass repairs is the search leg's
    MIRROR: older `memory_embeddings` rows can carry
    `active` for a row that is a duplicate in `loom.memory`, so the dense leg
    admits them into the pool and the truth read has to drop them after the
    vector sort. Re-emitting those mirror rows with `status='duplicate'` (a
    versioned insert, the `supersede_duplicates` pattern) lets the leg's own
    predicate skip them before the sort. `dry_run` (the default) reports the
    pairs, the orphans (canonical no longer active — see
    `memory.resolve_orphan_duplicates`) and the stale mirror rows without
    writing. WRITER-callable: it changes no value, key or lifecycle state.
    """
    import server.memory.store as memory_repo

    version = memory_repo.next_version()  # before the scan: the restamp is a retire
    pairs = await memory_repo.find_duplicate_pairs(
        input.org, input.namespace, key_prefix=input.prefix or "", limit=input.limit
    )
    canon_rows = await memory_repo.canonical_rows_by_ids(
        input.org,
        [input.namespace],
        [p.duplicate_of for p in pairs],
        # The pairs are the whole namespace's; the canonicals must be too.
        scope=WHOLE_NAMESPACE,
    )
    canonical_key = {r.canonical_id: r.memory_key for r in canon_rows}
    live = [p for p in pairs if p.duplicate_of in canonical_key]
    identities = {(p.agent, p.user_id, p.session_id, p.memory_key) for p in live}
    mirror = await memory_repo.embedding_mirror_for_keys(
        input.org, input.namespace, [p.memory_key for p in live]
    )
    stale = [
        r
        for r in mirror
        if (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"]))
        in identities
        and str(r.get("status") or "") != "duplicate"
    ]
    restamped = 0
    if not input.dry_run and stale:
        restamped = await memory_repo.restamp_embedding_rows(
            stale, status="duplicate", version=version
        )
    sample = [
        _DedupCollapsePair(
            duplicate_key=p.memory_key,
            canonical_key=canonical_key[p.duplicate_of],
        )
        for p in live[: input.sample]
    ]
    return MemoryDedupCollapseOutput(
        namespace=input.namespace,
        prefix=input.prefix or "",
        dry_run=bool(input.dry_run),
        pairs=len(pairs),
        canonical_live=len(live),
        orphans=len(pairs) - len(live),
        mirror_stale=len(stale),
        restamped=restamped,
        truncated=len(pairs) >= int(input.limit),
        sample=sample,
    )
