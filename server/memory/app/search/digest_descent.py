"""Digest descent: the sessions behind the pool's top session digests contribute
their own nearest facts."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.context import SearchContext
from server.memory.app.search.scoring import _anchor_rank_score
from server.memory.app.search.sql import _digest_descent_where, digest_descent_sql
from server.memory.app.search_tag_filter import stamp_row_tags as _stamp_row_tags
from server.memory.domain.search_types import hit_row_key, row_identity, row_key_of

log = logging.getLogger(__package__)


def digest_descent_enabled(input: Any) -> bool:
    """On unless the request's `digest_descent` turns it off."""
    v = getattr(input, "digest_descent", None)
    return True if v is None else bool(v)


def descent_targets(
    hits: list[Any], *, top_m: int, default_namespace: str
) -> list[tuple[str, str]]:
    """Pure: the (namespace, session_id) pairs behind the top-m session digests
    in the pool, by digest cosine, deduplicated. A digest's key is
    `summary:<session_id>`."""

    def _cos(h: Any) -> float:
        comps = getattr(h, "score_components", None) or {}
        return float(comps.get("summary_cosine", 0.0) or getattr(h, "rank_score", 0.0) or 0.0)

    digests = [
        h
        for h in hits
        if (getattr(h, "source", "") or "") == "summary"
        and str(getattr(h, "memory_key", "") or "").startswith("summary:")
    ]
    digests.sort(key=lambda h: -_cos(h))
    out: list[tuple[str, str]] = []
    for h in digests:
        sid = str(h.memory_key)[len("summary:") :]
        ns = str(getattr(h, "namespace", "") or default_namespace)
        if sid and sid != "-" and (ns, sid) not in out:
            out.append((ns, sid))
        if len(out) >= max(0, int(top_m)):
            break
    return out


async def _run_digest_descent(ctx: SearchContext) -> None:
    """Hierarchical descent from a session digest to its facts. A session digest that ranks
    among the pool's top-m summaries names a session; its own top-k facts by cosine join the
    pool as candidates the ANN legs did not reach. In additive modes a descended fact carries
    its own cosine as its raw score and competes on it; in rrf it enters at the pool floor.
    Never a filter, fail-open; on unless the request's `digest_descent` turns it off."""
    input = ctx.input
    if not digest_descent_enabled(input) or getattr(input, "recipe", "") == "fast":
        return
    from server.settings import settings as _dd

    top_m = int(_dd.memory_search_digest_descent_top_m)
    k = int(_dd.memory_search_digest_descent_k)
    targets = descent_targets(ctx.all_hits, top_m=top_m, default_namespace=input.namespace)
    if not targets:
        return
    ctx.actual_work["digest_descent_sessions"] = len(targets)
    present = {hit_row_key(h, input.namespace) for h in ctx.all_hits}
    pool_floor = min(
        (
            float(h.score_components.get("rank_score_raw", h.rank_score))
            for h in ctx.all_hits
            if (getattr(h, "source", "") or "fact") == "fact"
        ),
        default=0.0,
    )
    rrf = input.search_mode == "rrf"
    try:
        from server.core.ch import get_client as _get_ch_dd

        _ch = _get_ch_dd()

        async def _rows(ns: str, sid: str) -> tuple[str, str, list[dict[str, Any]]]:
            params: dict[str, Any] = {
                "org": input.org,
                "ns": ns,
                "sid": sid,
                "k": k,
                "qv": ctx.query_vec,
                "model": ctx.embedder.model_name,
            }
            sql = digest_descent_sql(" AND ".join(_digest_descent_where(input, params)))
            try:
                return ns, sid, await _ch.query_all(sql, params=params)
            except Exception as e:  # noqa: BLE001 — additive arm
                log.warning("digest descent failed for %s/%s (non-fatal): %s", ns, sid, e)
                return ns, sid, []

        results = await asyncio.gather(*[_rows(ns, sid) for ns, sid in targets])
        added = 0
        for ns, sid, rows in results:
            for row in rows:
                mk = str(row.get("memory_key") or "")
                if not mk:
                    continue
                row_key = row_key_of(ns, row)
                if row_key in present:
                    continue
                sim = float(row.get("similarity") or 0.0)
                raw = _anchor_rank_score(sim, pool_floor) if rrf else sim
                hit = _MemorySearchHit(
                    memory_key=mk,
                    memory_type=str(row.get("memory_type") or "semantic"),
                    version=int(row.get("version") or 0),
                    content_excerpt=str(row.get("content_excerpt") or ""),
                    distance=1.0 - sim,
                    similarity=sim,
                    rank_score=raw,
                    temporal_anchor=str(row.get("temporal_anchor") or ""),
                    temporal_confidence=float(row.get("temporal_confidence") or 0.0),
                    valid_at=str(row.get("valid_at") or ""),
                    created_at=str(row.get("created_at") or ""),
                    source="fact",
                    source_resource_id=str(row.get("source_resource_id") or ""),
                    namespace=ns,
                    session_id=sid,
                    score_components={
                        "semantic": sim,
                        "rank_score_raw": raw,
                        "rank_score": raw,
                        "digest_descent": 1.0,
                    },
                )
                _stamp_row_tags(hit, row.get("tags"))
                hit._row_identity = row_identity(row)
                present.add(row_key)
                ctx.all_hits.append(hit)
                added += 1
        ctx.actual_work["digest_descent_added"] = added
    except Exception as e:  # noqa: BLE001
        log.warning("digest descent stage failed (non-fatal): %s", e)
