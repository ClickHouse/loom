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
from server.memory.app import blacklist_guard
from server.memory.domain import assistant_rules
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
    support = await _assistant_turn_support(input.org, input.namespace, rows)
    entries = []
    for r in rows:
        r = {**r, **support.get(str(r["candidate_id"]), {})}
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
                supporter_sessions=int(r.get("supporter_sessions") or 0),
                supporter_writers=int(r.get("supporter_writers") or 0),
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


async def _assistant_turn_support(
    org: str, namespace: str, rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The support of each `assistant_turn` row, counted from
    loom.learnings_supporters: the headline count is distinct users, with the
    distinct sessions and writers beside it, the six-part supporter entries, and the earliest
    remaining supporter's quotes as the rationale (the candidate row holds none)."""
    by_id = {
        str(r["candidate_id"]): r
        for r in rows
        if r.get("source_kind") == assistant_rules.SOURCE_KIND
    }
    if not by_id:
        return {}
    stats = await learnings_repo.supporter_stats(org, namespace, list(by_id))
    out: dict[str, dict[str, Any]] = {}
    for s in stats:
        cid = str(s["candidate_id"])
        row = by_id[cid]
        prefix = f"{org}/{namespace}/{row.get('agent') or '-'}"
        out[cid] = {
            "supporter_count": int(s["users"]),
            "supporter_sessions": int(s["sessions"]),
            "supporter_writers": int(s["writers"]),
            "rationale": assistant_rules.rationale(str(s["asked"]), str(s["said"])),
            "confidence_avg": float(s["confidence"]),
            "supporters": [f"{prefix}/{u}/{sid}/{row['memory_key']}" for _, u, sid in s["pairs"]],
        }
    return out


async def handle_learnings_quality(
    input: LearningsQualityInput,
) -> LearningsQualityOutput:
    """Per-namespace approve-ratio rollup of the learnings queue.

    Scope: the namespaces the caller may read, as every org-wide rollup
    scopes them (`resolve_org_scope`: an org admin's view leaves out the
    restricted namespaces and collections it cannot read); a named namespace
    the transport gate has checked. Otherwise the panel leaks candidate
    counts and queue depths across the org's namespaces.
    """
    from server.auth.context import resolve_org_scope
    from server.core.ch import get_client

    params: dict[str, Any] = {"org": input.org}
    where = "org = {org:String}"
    if input.namespace:
        where += " AND namespace = {ns:String}"
        params["ns"] = input.namespace
    else:
        scope = await resolve_org_scope(input.org)
        if not scope.accessible:
            return LearningsQualityOutput(rows=[])
        where += scope.where
        params.update(scope.params)
    if input.source_kind:
        where += " AND source_kind = {sk:String}"
        params["sk"] = input.source_kind
    rows = await get_client().query_all(
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
    """Flip a candidate's status. On 'promote', write it to `destination`, or
    when unset to knowledge for an `assistant_turn` rule and to memory
    otherwise: `loom.shared` through memory.promote for a supporters
    candidate, a new row through memory.set for the rest.

    Reviewing a candidate again with the decision it already holds is a
    no-op (returns the existing status); rejecting a promoted candidate marks
    it rejected and leaves what the promotion wrote (delete a knowledge
    promotion's `learning://<candidate_id>` document with knowledge.delete to
    stop serving it). A bot's rule whose last supporter was erased answers 404.
    """
    from fastapi import HTTPException

    row = await learnings_repo.find_candidate(
        input.org,
        input.namespace,
        input.candidate_id,
    )
    # A bot rule with no supporter left (blanked once decided, deleted once pending
    # by the erasure that took its last supporter): there is no rule left to review,
    # so it answers as a missing candidate does.
    agent_rule = False
    support: list[dict[str, Any]] = []
    if row is not None and row.get("source_kind") == assistant_rules.SOURCE_KIND:
        agent_rule = True
        support = await learnings_repo.supporter_stats(
            input.org, input.namespace, [str(row["candidate_id"])]
        )
        if not row.get("proposed_value") or not support:
            row = None
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
        # A bot rule is promoted with the confidence the reviewer saw: the mean over
        # its supporters, not the filing session's own. Only the promotion reads it;
        # the status row below keeps the candidate's own columns.
        promoted = {**row, "confidence_avg": float(support[0]["confidence"])} if support else row
        destination = input.destination or ("knowledge" if agent_rule else "memory")
        if agent_rule:
            # A pattern added after the rule was filed still refuses it, on every
            # destination: memory.set checks the blacklist, knowledge.upsert does not.
            await blacklist_guard.enforce_on_insert(
                org=input.org, namespace=input.namespace, value=str(_proposed_value(row))
            )
        wants_memory = destination in ("memory", "both")
        wants_knowledge = destination in ("knowledge", "both")
        if wants_memory:
            await _do_promote(
                input.org,
                input.namespace,
                promoted,
                input.approver,
                input.reason,
            )
            promoted_to_shared = True
        if wants_knowledge:
            knowledge_source_uri = await _do_promote_to_knowledge(
                input.org,
                input.namespace,
                promoted,
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


def _proposed_value(candidate_row: dict[str, Any]) -> Any:
    """The candidate's value: `proposed_value` is JSON-encoded, or a plain string."""
    raw_value = candidate_row["proposed_value"]
    try:
        return json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (ValueError, TypeError):
        return raw_value


async def _do_promote(
    org: str,
    namespace: str,
    candidate_row: dict[str, Any],
    approver: str,
    reason: str,
) -> None:
    """Promote a candidate to a live memory.

    Three source flavors:
    - `memory_supporters`: an underlying loom.memory row already exists
      (multiple writers supported the same key) — route through
      `handle_memory_promote` so the institutional loom.shared row gets
      provenance + reversal window.
    - `conversation_seeder` (and any other no-source-memory path):
      there's no loom.memory row yet — the candidate WAS the proposal.
      Route through `handle_memory_set` so the row lands in loom.memory
      AND check_on_commit fires (enforces pinned-conflict reject + the
      semantic-conflict / canonical-merge / duplicate verdicts).
    - `assistant_turn` takes the same `handle_memory_set` path.
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
    """Seeder and bot-rule path: the candidate has no source memory yet. Write it
    as a new memory via handle_memory_set so check_on_commit enforces the same
    pinned-reject / dedup / canonical-merge verdicts a regular write would get. A
    seeder candidate lands at namespace scope under the approver; a bot rule
    (`assistant_turn`) as its agent's namespace-wide row (`user_id` '-'), which
    reads bounded to one user do not serve, with the supporters' mean confidence."""
    from server.memory.app.write import handle_memory_set

    deserialized = _proposed_value(candidate_row)

    # In both cases the approver is the row's `actor`, stamped from the review's token.
    if candidate_row.get("source_kind") == assistant_rules.SOURCE_KIND:
        agent, user_id = str(candidate_row.get("agent") or "-"), "-"
        # The candidate, not one supporter's session, is where it came from.
        source_trace = f"learning:{candidate_row['candidate_id']}"
    else:
        # A seeder candidate: namespace scope, under the approver's user_id.
        agent, user_id = "-", approver or "-"
        # The originating conversation, so the row keeps a path back to its thread
        # once the candidate ages out.
        seeded_thread = candidate_row.get("seeded_from_thread_id")
        source_trace = f"conversation:{seeded_thread}" if seeded_thread else ""
    set_input = MemorySetInput(
        org=org,
        namespace=namespace,
        agent=agent,
        user_id=user_id,
        session_id="-",
        memory_key=candidate_row["memory_key"],
        memory_type=candidate_row.get("memory_type") or "semantic",
        value=deserialized,
        tags=[
            f"source-kind:{candidate_row.get('source_kind', '')}",
            f"candidate-id:{candidate_row['candidate_id']}",
        ],
        confidence=float(candidate_row.get("confidence_avg") or 1.0),
        source_trace=source_trace,
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
    deserialized = _proposed_value(candidate_row)
    if isinstance(deserialized, str):
        content = deserialized
    else:
        content = json.dumps(deserialized, indent=2, default=str)

    source_uri = f"learning://{candidate_row['candidate_id']}"
    title = candidate_row.get("memory_key", "") or "promoted-learning"
    source_kind = candidate_row.get("source_kind", "") or "memory_supporters"
    tags = ["promoted-from-learning", f"source-kind:{source_kind}"]
    # A bot's rule names its agent in a tag: context.pack, memory.stack, loom_search and
    # memory.ask serve it to that agent and to namespace-wide (`*`) reads
    # (assistant_rules.serves_agent); knowledge.search is not filtered by agent, and a bot
    # finds its own rules with knowledge.search(tags=["agent:<id>"]). The document's
    # author is the reviewer's identity, stamped from the review's token.
    agent = (
        str(candidate_row.get("agent") or "") if source_kind == assistant_rules.SOURCE_KIND else ""
    )
    # The agent '-' is every agent in the namespace, so it names no bot to tag.
    if agent and agent != "-":
        tags.append(f"agent:{agent}")
    # A model extracted a bot's rule from users' conversations, so it is not the reviewer's
    # own content: `derived` is the nearest class a knowledge write may declare.
    source_class = "derived" if source_kind == assistant_rules.SOURCE_KIND else None
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
            tags=tags,
            source_class=source_class,
            metadata={
                "promoted_from_candidate_id": str(candidate_row["candidate_id"]),
                "rationale": rationale,
            },
        )
    )
    return source_uri
