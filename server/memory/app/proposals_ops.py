"""Handlers for memory.proposals and memory.proposal_decide."""

from __future__ import annotations

import logging
from typing import Any

from server.core.ch import get_client
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_where
from server.memory.api.models_proposals import (
    MemoryProposal,
    MemoryProposalDecideInput,
    MemoryProposalDecideOutput,
    MemoryProposalDecision,
    MemoryProposalsInput,
    MemoryProposalsOutput,
)
from server.settings import settings

log = logging.getLogger(__name__)


async def handle_memory_proposals(input: MemoryProposalsInput) -> MemoryProposalsOutput:
    from server.memory.app import proposals as _p

    ch = get_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "tombstoned_at IS NULL",
        "status = 'proposed'",
    ]
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "cap": settings.memory_proposals_scan_cap,
    }
    if input.prefix:
        where.append("startsWith(memory_key, {p:String})")
        params["p"] = input.prefix
    if input.tags:
        where.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = list(input.tags)
    where.extend(scope_where(request_scope(), params))
    # The scan reads ids only; values are read for the rows returned.
    rows = await ch.query_all(
        f"""
        SELECT memory_key, toString(memory_id) AS mid
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC
        LIMIT {{cap:UInt32}}
        {_sequential()}
        """,
        params=params,
    )
    policy = await _p.policy_for(input.org, input.namespace, ch=ch)
    # `ready` grades every row to choose which to return.
    wanted = rows if input.only == "ready" else rows[: input.limit]
    details = await _details(ch, where, params, [str(r["mid"]) for r in wanted])
    graded = [{**r, **details[str(r["mid"])]} for r in wanted if str(r["mid"]) in details]
    keys = [str(r["memory_key"]) for r in graded]
    counts = await _p.graded_counts(ch, input.org, input.namespace, keys)
    usage = await _usage_counts(ch, input.org, input.namespace, [str(r["mid"]) for r in graded])
    out: list[MemoryProposal] = []
    for r in graded:
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


async def _details(
    ch: Any, where: list[str], params: dict[str, Any], memory_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """What a listed proposal shows, for the scanned rows it returns, by memory id."""
    if not memory_ids:
        return {}
    rows = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS mid, agent, memory_type,
               -- a high value is readable by key only
               if(sensitivity = 'high', '', substring(value, 1, 400)) AS preview, sensitivity,
               tags, toString(created_at) AS created, conflict_with
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)} AND memory_id IN {{ids:Array(UUID)}}
        {_sequential()}
        """,
        params={**params, "ids": memory_ids},
    )
    return {str(r["mid"]): r for r in rows}


def _sequential() -> str:
    """A re-read right after a decision must not see the decided row on a lagging replica."""
    import server.memory.store as memory_repo

    return memory_repo._SEQUENTIAL if memory_repo.read_your_writes_enabled() else ""


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
