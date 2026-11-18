"""Fill mirror leg pools after admission, retaining no denied chunk text."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from server.core import pg, request_work
from server.core.ch import sequential_reads, set_query_tag
from server.core.diagnostics import private_work
from server.knowledge.app.search import HybridSearchQuery, _common_where
from server.knowledge.app.source_access import Admitted, ReadPlan
from server.knowledge.domain.access_list import candidate_keys
from server.knowledge.store import source_candidates as store


@dataclass(frozen=True)
class Hydrated:
    admitted: Admitted
    row: dict[str, Any]


def _family(item: Admitted) -> tuple[str, str]:
    return item.namespace, item.candidate["source_uri"].rsplit("#", 1)[0]


async def collect_leg(
    plan: ReadPlan,
    query: HybridSearchQuery,
    leg: store.Leg,
    *,
    limit: int,
    ordinary: set[tuple[str, str]] | None = None,
) -> list[Admitted]:
    """Close one ranked discovery stream when its admitted pool fills or it ends."""
    sources = {
        key: original
        for key, original in plan.discovery_sources.items()
        if original in query.namespaces
    }
    if not sources or limit <= 0:
        return []
    set_query_tag("knowledge.mirror.search")
    out: list[Admitted] = []
    seen: set[tuple[str, str, int]] = set()
    ordinary = ordinary or set()

    async def take(batch: list[dict[str, Any]]) -> None:
        for item in await plan.check(batch, chunks=True):
            family = _family(item)
            key = (*family, int(item.candidate["chunk_idx"]))
            if family not in ordinary and key not in seen:
                seen.add(key)
                out.append(item)
                if len(out) >= limit:
                    break

    try:
        with sequential_reads(), private_work():
            mirrors = tuple(sorted({ns for ns, _sid in sources}))
            where, params = _common_where(replace(query, namespaces=mirrors))
            where.append("hasAny(tags, {source_tags:Array(String)})")
            params.update(
                source_tags=sorted({"source-id:" + sid for _ns, sid in sources}),
                toks=query.tokens,
                keys=query.keys,
            )
            reader = plan.discovery_reader
            coarse = (
                None
                if pg.enabled()
                else candidate_keys(
                    subject=reader.subject if reader else "",
                    emails=reader.emails if reader else frozenset(),
                    domains=reader.domains if reader else frozenset(),
                )
            )
            sql, params = store.statement(where, params, leg=leg, access_keys=coarse)
            # Open and consume in this coroutine: acquiring all streams first can
            # deadlock when selected legs exceed the available stream slots.
            async with store.candidates(sql, params) as stream:
                batch = []
                async for row in stream:
                    batch.append(row)
                    if len(batch) == 64:
                        await take(batch)
                        batch = []
                        if len(out) >= limit:
                            break
                if batch and len(out) < limit:
                    await take(batch)
    except Exception:  # noqa: BLE001 — an unconditional discovery failure invalidates every mirror leg
        plan.failed = plan.unread = True
        request_work.mark_mirrored(unread=True)
    return [] if plan.failed or plan.unread else out


async def discover(
    plan: ReadPlan,
    query: HybridSearchQuery,
    *,
    legs: tuple[store.Leg, ...],
    limit: int,
    ordinary: set[tuple[str, str]] | None = None,
) -> dict[store.Leg, list[Admitted]]:
    """Select all discovery queries from the request, independently of hidden matches."""
    pools = await asyncio.gather(
        *(collect_leg(plan, query, leg, limit=limit, ordinary=ordinary) for leg in legs)
    )
    if plan.failed or plan.unread:
        return {}
    return await recheck(plan, dict(zip(legs, pools, strict=True)))


def _chunk_key(row: dict[str, Any]) -> tuple[str, str, int, int, int]:
    return (
        row["namespace"],
        row["source_uri"],
        int(row["chunk_idx"]),
        int(row["doc_version"]),
        int(row["revision"]),
    )


async def hydrate(
    plan: ReadPlan,
    pools: dict[store.Leg, list[Admitted]],
    *,
    model: str,
    embedded_only: bool = True,
) -> dict[store.Leg, list[Hydrated]]:
    """Keep text private until its exact key passes the post-fetch permission check."""
    if plan.failed or plan.unread:
        return {}
    set_query_tag("knowledge.mirror.hydrate")
    try:
        with private_work(), sequential_reads():
            checked = await recheck(plan, pools)
            keys = list(
                dict.fromkeys(
                    _chunk_key(item.candidate) for pool in checked.values() for item in pool
                )
            )
            rows = {}
            for start in range(0, len(keys), 64):
                batch = keys[start : start + 64]
                wanted = set(batch)
                for row in await store.chunks(plan.org, model, batch, embedded_only=embedded_only):
                    key = _chunk_key(row)
                    # MergeTree retries can leave physical duplicates. Ambiguous
                    # hydration discards the leg rather than picking one payload.
                    if key not in wanted or key in rows:
                        raise ValueError("ambiguous hydrated chunk")
                    rows[key] = row
            checked = await recheck(plan, checked)
            return {
                leg: [
                    Hydrated(item, rows[_chunk_key(item.candidate)])
                    for item in pool
                    if _chunk_key(item.candidate) in rows
                ]
                for leg, pool in checked.items()
            }
    except Exception:  # noqa: BLE001 — conditional hydration must not expose partial mirror results
        plan.failed = True
        return {}


async def recheck(
    plan: ReadPlan, pools: dict[store.Leg, list[Admitted]]
) -> dict[store.Leg, list[Admitted]]:
    """Re-read retained permissions before content use, keeping each leg's own scores."""
    set_query_tag("knowledge.mirror.recheck")
    unique = {
        _chunk_key(item.candidate): item.candidate for pool in pools.values() for item in pool
    }
    candidates = list(unique.values())
    admitted = {}
    for start in range(0, len(candidates), 64):
        checked = await plan.check(candidates[start : start + 64], chunks=True)
        admitted.update({_chunk_key(item.candidate): item for item in checked})
    if plan.failed or plan.unread:
        return {}
    # A listing or group snapshot may expire while another batch is rechecked.
    admitted = {key: item for key, item in admitted.items() if plan.current(item)}
    return {
        leg: [
            replace(
                admitted[_chunk_key(item.candidate)],
                namespace=item.namespace,
                candidate=item.candidate,
            )
            for item in pool
            if _chunk_key(item.candidate) in admitted
        ]
        for leg, pool in pools.items()
    }
