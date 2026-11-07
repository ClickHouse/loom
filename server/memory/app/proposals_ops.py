"""Handlers for memory.proposals and memory.proposal_decide."""

from __future__ import annotations

import logging
from typing import Any

from server.core.ch import get_client
from server.identity.app.request_scope import request_scope
from server.memory.api.models_proposals import (
    MemoryProposal,
    MemoryProposalDecideInput,
    MemoryProposalDecideOutput,
    MemoryProposalDecision,
    MemoryProposalsInput,
    MemoryProposalsOutput,
)
from server.memory.store import proposals as proposals_repo
from server.settings import settings

log = logging.getLogger(__name__)


async def handle_memory_proposals(input: MemoryProposalsInput) -> MemoryProposalsOutput:
    from server.memory.app import proposals as _p

    ch = get_client()
    rows = await proposals_repo.scan_proposals(
        ch,
        input.org,
        input.namespace,
        scope=request_scope(),
        cap=settings.memory_proposals_scan_cap,
        prefix=input.prefix,
        tags=input.tags,
    )
    policy = await _p.policy_for(input.org, input.namespace, ch=ch)
    # `ready` grades every row to choose which to return.
    wanted = rows if input.only == "ready" else rows[: input.limit]
    details = await proposals_repo.proposal_details(ch, input.org, input.namespace, wanted)
    listed = [{**r, **details[str(r["mid"])]} for r in wanted if str(r["mid"]) in details]
    keys = [str(r["memory_key"]) for r in listed]
    counts = await _p.graded_counts(ch, input.org, input.namespace, keys)
    usage = await _usage_counts(ch, input.org, input.namespace, [str(r["mid"]) for r in listed])
    out: list[MemoryProposal] = []
    for r in listed:
        key, mid = str(r["memory_key"]), str(r["mid"])
        c = counts.get(key, _p.GradedCounts())
        ready = _p.policy_verdict(policy, c) or ""
        if input.only == "ready" and not ready:
            continue
        used, harm = usage.get(mid, (0, 0))
        out.append(
            MemoryProposal(
                memory_key=key,
                memory_id=mid,
                agent=str(r.get("agent") or ""),
                memory_type=str(r.get("memory_type") or ""),
                value_preview=str(r.get("preview") or ""),
                sensitivity=str(r.get("sensitivity") or ""),
                tags=[str(t) for t in (r.get("tags") or [])],
                created_at=str(r.get("created") or ""),
                graded_passes=c.passes,
                graded_fails=c.fails,
                last_graded_at=c.last_graded_at,
                used_count=used,
                harm_count=harm,
                ready=ready,
                conflict_with=[str(x) for x in (r.get("conflict_with") or []) if x],
            )
        )
    return MemoryProposalsOutput(
        proposals=out[: input.limit],
        total=len(rows),
        policy=policy,
    )


async def _usage_counts(
    ch: Any, org: str, namespace: str, memory_ids: list[str]
) -> dict[str, tuple[int, int]]:
    """(used, harm) per memory id from the usage totals; {} when unreadable
    (the listing never fails on a counter)."""
    if not memory_ids:
        return {}
    try:
        rows = await ch.query_all(
            """
            SELECT toString(memory_id) AS mid, sum(used_count) AS used, sum(harm_count) AS harm
            FROM loom.memory_usage_totals
            WHERE org = {org:String} AND namespace = {ns:String}
              AND memory_id IN {ids:Array(UUID)}
            GROUP BY memory_id
            """,
            params={"org": org, "ns": namespace, "ids": memory_ids},
        )
    except Exception as e:  # noqa: BLE001 — proposals list without usage
        log.warning("memory.proposals: usage counts unreadable for %s: %s", namespace, e)
        return {}
    return {str(r["mid"]): (int(r.get("used") or 0), int(r.get("harm") or 0)) for r in rows}


async def handle_memory_proposal_decide(
    input: MemoryProposalDecideInput,
) -> MemoryProposalDecideOutput:
    from server.memory.app import proposals as _p
    from server.memory.app.provenance import writer_actor

    results = await _p.decide(
        input.org,
        input.namespace,
        list(input.memory_keys),
        input.decision,
        actor=writer_actor() or "-",
        reason=input.reason,
    )
    return MemoryProposalDecideOutput(
        decided=sum(1 for r in results if r.decided),
        results=[
            MemoryProposalDecision(
                memory_key=r.memory_key, decided=r.decided, status=r.status, reason=r.reason
            )
            for r in results
        ],
    )
