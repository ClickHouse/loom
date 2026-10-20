"""learnings.{list, review} handlers."""

from __future__ import annotations

import json
from typing import Any, Literal

from server.core.datetimes import naive_utc_now
from server.memory.api.models_crud import MemorySetInput
from server.memory.api.models_learnings import (
    LearningsListInput,
    LearningsListOutput,
    LearningsQualityInput,
    LearningsQualityOutput,
    LearningsQualityRow,
    LearningsReviewInput,
    LearningsReviewOutput,
    _LearningEntry,
)
from server.memory.api.models_lifecycle import MemoryPromoteInput
from server.memory.domain.assistant_rules import SOURCE_KIND as ASSISTANT_TURN
from server.memory.store import learnings as learnings_repo
from server.memory.store.rows import next_version


async def handle_learnings_list(
    input: LearningsListInput,
) -> LearningsListOutput:
    """Page through the review queue. Defaults to status='pending'."""

    status_filter = input.status or "pending"
    rows = await learnings_repo.list_by_status(
        input.org,
        input.namespace,
        status_filter,
        limit=input.limit,
        source_kind=input.source_kind,
    )
    entries = []
    for r in rows:
        try:
            proposed_value = json.loads(r["proposed_value"]) if r["proposed_value"] else None
        except (ValueError, TypeError):
            proposed_value = r["proposed_value"]
        entries.append(
            _LearningEntry(
                candidate_id=str(r["candidate_id"]),
                memory_key=r["memory_key"],
                memory_type=r["memory_type"],
                proposed_value=proposed_value,
                supporter_count=int(r["supporter_count"]),
                confidence_avg=float(r["confidence_avg"]),
                supporters=list(r["supporters"]),
                rationale=r["rationale"],
                source_kind=r.get("source_kind", "memory_supporters"),
                agent=str(r.get("agent") or ""),
                status=r["status"],
                proposed_at=r["proposed_at"],
                decided_at=r["decided_at"],
                decided_by=r["decided_by"],
                decision_reason=r["decision_reason"],
            )
        )
    return LearningsListOutput(rows=entries)


async def handle_learnings_quality(
    input: LearningsQualityInput,
) -> LearningsQualityOutput:
    """Per-namespace approve-ratio rollup of the learnings queue.

    Scope: the caller sees ONLY namespaces they have access to (JWT
    namespaces ∪ accepted grants), otherwise the panel leaks candidate
    counts + queue depths across the org's namespaces.
    """
    from server.auth.context import current_auth
    from server.core.ch import get_client

    # ORG_ADMIN+ sees org-wide; everyone else only their member namespaces.
    # No auth context (single-user dev) falls through to org-wide.
    from server.operations.base import Role as _R

    allowed_ns: list[str] | None = None
    auth = current_auth()
    if auth is not None and auth.subject:
        platform_view = any(r >= _R.ORG_ADMIN for r in getattr(auth, "roles", []) or [])
        if not platform_view:
            from server.auth.context import rollup_member_namespaces

            allowed_ns = await rollup_member_namespaces(input.org)

    ch = get_client()
    params: dict[str, Any] = {"org": input.org}
    where = "org = {org:String}"
    if input.namespace:
        where += " AND namespace = {ns:String}"
        params["ns"] = input.namespace
    elif allowed_ns is not None:
        # No namespace access → empty result, not WHERE namespace IN ().
        if not allowed_ns:
            return LearningsQualityOutput(rows=[])
        where += " AND namespace IN {ns_list:Array(String)}"
        params["ns_list"] = allowed_ns
    if input.source_kind:
        where += " AND source_kind = {sk:String}"
        params["sk"] = input.source_kind
    rows = await ch.query_all(
        f"""
        SELECT
            namespace,
            countIf(status='pending')  AS pending,
            countIf(status='promoted') AS promoted,
            countIf(status='rejected') AS rejected,
            if(countIf(status IN ('promoted','rejected')) > 0,
               countIf(status='promoted') / countIf(status IN ('promoted','rejected')),
               0.0) AS approve_ratio
        FROM loom.learnings_queue FINAL
        WHERE {where}
        GROUP BY namespace
        ORDER BY pending DESC, namespace
        """,
        params=params,
    )
    return LearningsQualityOutput(
        rows=[
            LearningsQualityRow(
                namespace=str(r["namespace"]),
                pending=int(r["pending"]),
                promoted=int(r["promoted"]),
                rejected=int(r["rejected"]),
                approve_ratio=float(r["approve_ratio"]),
            )
            for r in rows
        ]
    )


async def handle_learnings_review(
    input: LearningsReviewInput,
) -> LearningsReviewOutput:
    """Flip a candidate's status. On 'promote', fan out via the existing
    `handle_memory_promote` path so the institutional row in
    `loom.shared` gets the same provenance + reversal-window the
    explicit promotion flow gives it.

    Idempotent: re-reviewing an already-decided candidate is a no-op
    (returns the existing status).
    """
    from fastapi import HTTPException

    row = await learnings_repo.find_candidate(
        input.org,
        input.namespace,
        input.candidate_id,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RESOURCE_NOT_FOUND",
                "message": f"candidate not found: {input.candidate_id}",
            },
        )

    now = naive_utc_now()
    target_status: Literal["promoted", "rejected"] = (
        "promoted" if input.decision == "promote" else "rejected"
    )

    if row["status"] == target_status:
        return LearningsReviewOutput(
            candidate_id=str(row["candidate_id"]),
            new_status=target_status,
            promoted_to_shared=False,
            decided_at=now,
        )

    # Route promotion to memory, knowledge, or both.
    promoted_to_shared = False
    promoted_to_knowledge = False
    knowledge_source_uri = ""
    if input.decision == "promote":
        wants_memory = input.destination in ("memory", "both")
        wants_knowledge = input.destination in ("knowledge", "both")
        if wants_memory:
            await _do_promote(
                input.org,
                input.namespace,
                row,
                input.approver,
                input.reason,
            )
            promoted_to_shared = True
        if wants_knowledge:
            knowledge_source_uri = await _do_promote_to_knowledge(
                input.org,
                input.namespace,
                row,
                input.approver,
                input.reason,
            )
            promoted_to_knowledge = True

    # ReplacingMergeTree(version): new status row; FINAL reads the latest, so every
    # column is carried from the candidate — `seeded_from_*` keeps its conversation
    # back-pointer (read by memory.lineage), and the support counts and proposal time
    # stay what learnings.list and the lineage's candidate report.
    await learnings_repo.insert_row(
        {
            "candidate_id": str(row["candidate_id"]),
            "org": input.org,
            "namespace": input.namespace,
            "memory_key": row["memory_key"],
            "memory_type": row["memory_type"],
            "proposed_value": row["proposed_value"],
            "supporter_count": int(row.get("supporter_count") or 0),
            "confidence_avg": float(row.get("confidence_avg") or 0.0),
            "supporters": list(row["supporters"]),
            "rationale": row["rationale"],
            "source_kind": row.get("source_kind", "memory_supporters"),
            "agent": str(row.get("agent") or ""),
            "supporter_users": list(row.get("supporter_users") or []),
            "seeded_from_thread_id": row.get("seeded_from_thread_id"),
            "seeded_from_msg_id": row.get("seeded_from_msg_id"),
            "status": target_status,
            "proposed_at": row.get("proposed_at") or now,
            "decided_at": now,
            "decided_by": input.approver,
            "decision_reason": input.reason,
            "version": next_version(),
        }
    )

    return LearningsReviewOutput(
        candidate_id=str(row["candidate_id"]),
        new_status=target_status,
        promoted_to_shared=promoted_to_shared,
        promoted_to_knowledge=promoted_to_knowledge,
        knowledge_source_uri=knowledge_source_uri,
        decided_at=now,
    )


async def _do_promote(
    org: str,
    namespace: str,
    candidate_row: dict[str, Any],
    approver: str,
    reason: str,
) -> None:
    """Promote a candidate to a live memory.

    Two source flavors:
    - `memory_supporters`: an underlying loom.memory row already exists
      (multiple writers supported the same key) — route through
      `handle_memory_promote` so the institutional loom.shared row gets
      provenance + reversal window.
    - `conversation_seeder` (and any other no-source-memory path):
      there's no loom.memory row yet — the candidate WAS the proposal.
      Route through `handle_memory_set` so the row lands in loom.memory
      AND check_on_commit fires (enforces pinned-conflict reject + the
      semantic-conflict / canonical-merge / duplicate verdicts).
    """
    source_kind = candidate_row.get("source_kind") or "memory_supporters"

    if source_kind == "memory_supporters":
        await _promote_via_shared(org, namespace, candidate_row, approver, reason)
    else:
        await _promote_via_memory_set(org, namespace, candidate_row, approver, reason)


async def _promote_via_shared(
    org: str,
    namespace: str,
    candidate_row: dict[str, Any],
    approver: str,
    reason: str,
) -> None:
    """Supporters tuple points at an existing loom.memory row; promote it
    to loom.shared via handle_memory_promote."""
    from server.memory.app.promote import handle_memory_promote

    supporters = list(candidate_row["supporters"])
    if not supporters:
        raise RuntimeError(f"candidate {candidate_row['candidate_id']} has no supporters")
    latest = supporters[-1]
    parts = latest.split("/")
    if len(parts) != 6:
        raise RuntimeError(f"malformed supporter tuple: {latest!r}")
    _org, _ns, agent, user_id, session_id, memory_key = parts

    rationale = (
        f"reviewed-and-promoted by {approver}: {reason}"
        if reason
        else f"reviewed-and-promoted by {approver}"
    )

    promote_input = MemoryPromoteInput(
        org=org,
        namespace=namespace,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        memory_key=memory_key,
        target_key=candidate_row["memory_key"],
        rationale=rationale,
        approver=approver,
        reversible_for_days=7,
        # Thread candidate_id to loom.shared so memory.lineage resolves it
        # with match_confidence='exact' instead of a time-window match.
        promoted_from_candidate_id=str(candidate_row["candidate_id"]),
    )
    await handle_memory_promote(promote_input)


async def _promote_via_memory_set(
    org: str,
    namespace: str,
    candidate_row: dict[str, Any],
    approver: str,
    reason: str,
) -> None:
    """Seeder path: candidate has no source memory yet. Write it as a
    new memory via handle_memory_set so check_on_commit enforces the
    same pinned-reject / dedup / canonical-merge verdicts a regular
    write would get."""
    from server.memory.app.write import handle_memory_set

    raw_value = candidate_row["proposed_value"]
    try:
        deserialized = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (ValueError, TypeError):
        deserialized = raw_value

    # Seeder supporters encode the originating thread, not a memory row.
    # Sentinel "-" puts the row at namespace scope; approver lands as
    # user_id so audit retains accountability. A rule an assistant stated about
    # itself becomes its agent's memory for every user (user_id "-"); the
    # approver is the row's `actor`, stamped from the review's token.
    # Persist the originating conversation on the durable memory row's
    # source_trace (the seeded_from link otherwise lives only on the
    # learnings_queue candidate, which ages out) — so a promoted fact keeps a
    # path back to the thread that produced it, for audit + "memories from this
    # thread".
    seeded_thread = candidate_row.get("seeded_from_thread_id")
    agent_rule = candidate_row.get("source_kind") == ASSISTANT_TURN
    set_input = MemorySetInput(
        org=org,
        namespace=namespace,
        agent=str(candidate_row.get("agent") or "-") if agent_rule else "-",
        user_id="-" if agent_rule else (approver or "-"),
        session_id="-",
        memory_key=candidate_row["memory_key"],
        memory_type=candidate_row.get("memory_type") or "semantic",
        value=deserialized,
        tags=[
            f"source-kind:{candidate_row.get('source_kind', '')}",
            f"candidate-id:{candidate_row['candidate_id']}",
        ],
        confidence=float(candidate_row.get("confidence_avg") or 1.0),
        source_trace=f"conversation:{seeded_thread}" if seeded_thread else "",
    )
    # 409 PINNED_CONFLICT bubbles up through learnings.review.
    await handle_memory_set(set_input)


async def _do_promote_to_knowledge(
    org: str,
    namespace: str,
    candidate_row: dict[str, Any],
    approver: str,
    reason: str,
) -> str:
    """Route a learnings candidate to the knowledge zone.

    source_uri is `learning://<candidate_id>` so an operator can find /
    delete the doc later via knowledge.delete. Returns the source_uri.
    """
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app.writes import handle_knowledge_upsert

    # Deserialize JSON-encoded proposed_value; pretty-print non-strings
    # so the chunker has readable content.
    raw_value = candidate_row["proposed_value"]
    try:
        deserialized = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (ValueError, TypeError):
        deserialized = raw_value
    if isinstance(deserialized, str):
        content = deserialized
    else:
        content = json.dumps(deserialized, indent=2, default=str)

    source_uri = f"learning://{candidate_row['candidate_id']}"
    title = candidate_row.get("memory_key", "") or "promoted-learning"
    source_kind = candidate_row.get("source_kind", "") or "memory_supporters"
    rationale = f"reviewed-and-promoted-to-knowledge by {approver}" + (
        f": {reason}" if reason else ""
    )

    await handle_knowledge_upsert(
        KnowledgeUpsertInput(
            org=org,
            namespace=namespace,
            content=content,
            source_uri=source_uri,
            title=title,
            tags=["promoted-from-learning", f"source-kind:{source_kind}"],
            metadata={
                "promoted_from_candidate_id": str(candidate_row["candidate_id"]),
                "rationale": rationale,
            },
        )
    )
    return source_uri
