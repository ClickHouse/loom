"""The key-match arm: rows whose memory_key names the attribute the question asks
for, matched on stemmed key parts, and the gate that skips it when it cannot
change the served window."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app.search.context import SearchContext
from server.memory.app.search.scoring import _anchor_rank_score, rows_per_key
from server.memory.app.search.sql import (
    _key_match_where,
    key_match_component_sql,
    key_match_precheck_sql,
    key_match_sql,
)
from server.memory.app.search_tag_filter import stamp_row_tags as _stamp_row_tags
from server.memory.domain.search_types import hit_row_key, row_identity

log = logging.getLogger(__package__)


def _key_match_stop(token: str) -> bool:
    return token in _KEY_MATCH_STOP


_KEY_MATCH_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "is",
        "are",
        "was",
        "were",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "do",
        "does",
        "did",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "about",
        "from",
        "and",
        "or",
        "we",
        "know",
        "current",
        "currently",
        "user",
        "users",
        "s",
        "this",
        "that",
        "it",
        "its",
        "have",
        "has",
        "had",
        "many",
        "much",
        "any",
        "there",
        "be",
        "been",
        "non",
        "not",
        "will",
        "would",
        "should",
        "can",
        "could",
        "you",
        "your",
        "they",
        "them",
        "their",
        "i",
        "my",
        "me",
        "doe",
    }
)


def key_match_query_stems(query: str) -> list[str]:
    """Snowball stems of the query's content tokens (stop words and single
    characters dropped), the tokens a memory key's `_`-separated parts are
    matched against."""
    from server.memory.domain.lemmatize import lemma_tokens, snowball_stem_tokens

    toks = [t for t in lemma_tokens(query) if len(t) >= 2 and not _key_match_stop(t)]
    stems = set(snowball_stem_tokens(toks))
    # Snowball leaves acronym plurals alone ("prs" stays "prs" while the key
    # says "pr"); add the singular so those keys still match.
    for st in list(stems):
        if len(st) >= 3 and st.endswith("s") and not st.endswith("ss"):
            stems.add(st[:-1])
    return sorted(stems)


def key_match_mode(input: MemorySearchInput) -> str:
    """`always` | `gated` | `off` for this request. The per-request bool `key_match` wins
    (True → always, False → off), then the per-request `key_match_mode`, then the deployment's
    `LOOM_MEMORY_SEARCH_KEY_MATCH_MODE`."""
    v = input.key_match
    if v is not None:
        return "always" if bool(v) else "off"
    if input.key_match_mode is not None:
        return input.key_match_mode
    from server.settings import settings as _s

    return _s.memory_search_key_match_mode


def key_match_enabled(input: MemorySearchInput) -> bool:
    return key_match_mode(input) != "off"


def key_match_key_qualifies(memory_key: str, stems: set[str]) -> bool:
    """The arm's SQL predicate on one key, in Python and unweighted: the
    `_`-split, Snowball-stemmed parts share at least two stems with the
    query, or every part of a short key is one (`matched >= 2 OR (matched
    >= 1 AND matched = key_parts)`). Without the idf floor this is a
    superset of what the statement returns — it says whether a pooled row
    *could* gain the component, never that it will not."""
    from server.memory.domain.lemmatize import snowball_stem_tokens

    parts = str(memory_key or "").lower().split("_")
    key_stems = snowball_stem_tokens(parts)
    matched = sum(1 for t in key_stems if t in stems)
    return matched >= 2 or (matched >= 1 and matched == len(key_stems))


# Planes the assembler places by band rather than by fused score; they do not
# fill the window ahead of a fact and do not count toward the thin-pool test.
_KEY_MATCH_BANDED_PLANES = frozenset({"summary", "entity_summary", "chunk"})


def key_match_gate(
    *,
    query: str,
    stems: list[str],
    search_mode: str,
    recipe: str,
    external_rerank: bool,
    pool: list[tuple[str, str, str]],
    per_key_cap: int,
    top_k: int,
) -> str:
    """Why the gated arm must run for this search, or "" when it may be
    skipped. Pure; cheapest test first.

    The arm changes a served window in exactly two ways: a pooled row gains the `key_match`
    component (a boost of at most 0.03 cosine), or a row the pool missed is added at
    `pool_floor - 1e-6 - (1 - sim)` — at least ~0.2 below the weakest pooled row, so in an
    additive pool it is served only when the pool is too thin to fill top_k, or when a later
    stage weighs it by something other than its score: an RRF arm (1/(60 + rank), the same
    scale as a main-leg vote), a reranker, or the identifier pin — which lifts every row whose
    key or text carries the identifier and keeps their order, so an added row lands under the
    pooled rows that carry it and is served only when those are fewer than top_k. Each of
    those is a reason; the boost case is the pooled-key pre-check. `pool` is the pool before
    the arm as (memory_key, text, source) triples; `per_key_cap` the final block's rows per key
    (0 = none). The thin-pool test counts fact-plane rows only: summaries, entity summaries and
    chunks are placed by band in the block, not by fused score, so an added fact is served past
    them even when the pool as a whole holds top_k rows (seen at k = 30 on a chat corpus). A
    query with fewer than two stems never runs the arm and is not this function's business."""
    if search_mode == "rrf":
        return "rrf"
    if external_rerank or recipe == "quality":
        return "rerank"

    def _rows_after_cap(rows: list[tuple[str, str, str]]) -> int:
        per_key: dict[str, int] = {}
        for mk, _, _ in rows:
            per_key[mk] = per_key.get(mk, 0) + 1
        return sum(min(per_key_cap, n) if per_key_cap > 0 else n for n in per_key.values())

    if _rows_after_cap([r for r in pool if r[2] not in _KEY_MATCH_BANDED_PLANES]) < int(top_k):
        return "thin_pool"
    from server.memory.domain.query_planner import identifier_tokens

    idents = [t.lower() for t in identifier_tokens(query or "") if t]
    if idents:
        carrying = [
            row
            for row in pool
            if any(t in (row[1] or "").lower() or t in (row[0] or "").lower() for t in idents)
        ]
        if _rows_after_cap(carrying) < int(top_k):
            return "identifier"
    stem_set = set(stems)
    seen: set[str] = set()
    for mk, _, _ in pool:
        if mk in seen:
            continue
        seen.add(mk)
        if key_match_key_qualifies(mk, stem_set):
            return "pool_key"
    return ""


async def _run_key_match_pool(ctx: SearchContext) -> None:
    """The memory key names the attribute the question asks for (`user_task_worktree` ↔ "task
    worktree path"), and outside lexical mode no other leg reads it. This arm matches the
    query's stemmed content tokens against the key's `_`-separated parts in ClickHouse, pulls
    the rows whose key shares at least two stems (or every stem of a short key), and adds them
    to the pool as candidates with a `key_match` component the fusion pass turns into a boost.
    Rows already in the pool only gain the component. Additive and fail-open; the mode
    (`key_match_mode`: gated by default) decides whether it runs."""
    input = ctx.input
    mode = key_match_mode(input)
    if mode == "off" or getattr(input, "recipe", "") == "fast":
        return
    stems = key_match_query_stems(input.query)
    if len(stems) < 2:
        return
    ctx.actual_work["key_match_stems"] = len(stems)
    ctx.actual_work["key_match_mode"] = mode
    component_only = False
    if mode == "gated":
        # Skip the read when it cannot change the served window.
        reason = key_match_gate(
            query=str(input.query or ""),
            stems=stems,
            search_mode=str(getattr(input, "search_mode", "") or ""),
            recipe=str(getattr(input, "recipe", "") or ""),
            external_rerank=bool(ctx.endpoint),
            pool=[
                (
                    str(h.memory_key or ""),
                    str(getattr(h, "content_excerpt", "") or ""),
                    str(getattr(h, "source", "fact") or "fact"),
                )
                for h in ctx.all_hits
            ],
            per_key_cap=rows_per_key(ctx.query_shape),
            top_k=int(input.top_k),
        )
        ctx.actual_work["key_match_gate"] = reason or "skip"
        if not reason:
            return
        # Only a pooled row can be affected: read the strengths of the pooled
        # keys, not the namespace's wide rows (`key_match_component_sql`).
        component_only = reason == "pool_key"
    # Every pooled row sharing the key gains the component: extraction writes
    # the same key once per session, so a pool holds several rows per key.
    seen: dict[tuple[str, str], list[Any]] = {}
    for h in ctx.all_hits:
        seen.setdefault((h.namespace or input.namespace, h.memory_key), []).append(h)
    pooled = {hit_row_key(h, input.namespace) for h in ctx.all_hits}
    _pool_floor = min(
        (float(h.score_components.get("rank_score_raw", h.rank_score)) for h in ctx.all_hits),
        default=0.0,
    )
    try:
        from server.core.ch import get_client as _get_ch_k
        from server.settings import settings as _settings_km

        _ch = _get_ch_k()
        limit = max(20, input.top_k * 2)

        async def _rows_for_ns(ns: str) -> tuple[str, list[dict[str, Any]], bool]:
            params: dict[str, Any] = {
                "org": input.org,
                "ns": ns,
                "stems": stems,
                "limit": limit,
                # a key holds a row per writer and session: as many as the window keeps
                "per_key": rows_per_key(ctx.query_shape) or limit,
                "qv": ctx.query_vec,
                "model": ctx.embedder.model_name,
                "min_strength": float(_settings_km.memory_search_key_match_min_strength),
            }
            where = _key_match_where(input, params)
            # Key parts are stemmed in ClickHouse with the same Snowball the
            # query side used, so "worktree_path" meets "worktree path". Each
            # stem is weighted by its inverse frequency across the namespace's
            # keys: `user`, `pr`, `ci` sit in thousands of keys and say little;
            # `worktree` sits in a handful and says which row this is. The
            # strength is the share of the query's weighted stems the key covers.
            # Every pass reads under FINAL (a highest-version predicate instead saves bytes but no
            # latency). The tombstone filter sits in `where`, after the merge. The wide scan drops
            # keys sharing no stem in PREWHERE, before their vectors are read (`key_match_sql`).
            if component_only:
                pool_keys = sorted({mk for (_ns, mk) in seen if _ns == ns and mk})
                if not pool_keys:
                    return ns, [], False
                params["pool_keys"] = pool_keys
                params["limit"] = len(pool_keys)
                params.pop("qv", None)  # no vector column is read: the vector literal stays home
                sql = key_match_component_sql(" AND ".join(where))
            else:
                # A keys-only pass first (no FINAL; lossless, every version of a row has the
                # same key); the wide statement runs only when some key can qualify. A failed
                # pre-check falls through to the full arm.
                try:
                    probe = await _ch.query_all(
                        key_match_precheck_sql(),
                        params={"org": input.org, "ns": ns, "stems": stems},
                    )
                except Exception as e:  # noqa: BLE001 — additive arm
                    log.warning("key-match pre-check failed for ns=%s (non-fatal): %s", ns, e)
                    probe = [{"any_key": 1}]
                if not probe:
                    return ns, [], True
                sql = key_match_sql(" AND ".join(where))
            try:
                return ns, await _ch.query_all(sql, params=params), False
            except Exception as e:  # noqa: BLE001 — additive arm
                log.warning("key-match arm failed for ns=%s (non-fatal): %s", ns, e)
                return ns, [], False

        results = await asyncio.gather(*[_rows_for_ns(ns) for ns in ctx.namespaces])
        ctx.actual_work["key_match_precheck_skipped"] = sum(
            1 for (_ns, _rows, skipped) in results if skipped
        )
        added = 0
        for ns, rows, _skipped in results:
            for row in rows:
                mk = str(row.get("memory_key") or "")
                if not mk:
                    continue
                # idf-weighted share of the query's stems the key covers,
                # computed in ClickHouse against the namespace's own keys.
                strength = max(0.0, min(1.0, float(row.get("strength") or 0.0)))
                for eh in seen.get((ns, mk), []):
                    eh.score_components["key_match"] = max(
                        float(eh.score_components.get("key_match", 0.0)), strength
                    )
                if component_only:
                    continue  # the statement only scores the pooled keys
                row_key = (ns, str(row["agent"]), str(row["user_id"]), str(row["session_id"]), mk)
                if row_key in pooled:
                    continue
                sim = float(row.get("similarity") or 0.0)
                hit = _MemorySearchHit(
                    memory_key=mk,
                    memory_type=str(row.get("memory_type") or "semantic"),
                    content_excerpt=str(row.get("content_excerpt") or ""),
                    distance=1.0 - sim,
                    similarity=sim,
                    rank_score=_anchor_rank_score(sim, _pool_floor),
                    temporal_anchor=str(row.get("temporal_anchor") or ""),
                    temporal_confidence=float(row.get("temporal_confidence") or 0.0),
                    valid_at=str(row.get("valid_at") or ""),
                    created_at=str(row.get("created_at") or ""),
                    source="anchor",
                    session_id=str(row.get("session_id") or ""),
                    source_resource_id=str(row.get("source_resource_id") or ""),
                    namespace=ns,
                    score_components={"key_match": strength},
                )
                _stamp_row_tags(hit, row.get("tags"))
                hit._row_identity = row_identity(row)
                seen.setdefault((ns, mk), []).append(hit)
                pooled.add(row_key)
                ctx.all_hits.append(hit)
                added += 1
        ctx.actual_work["key_match_added"] = added
        if component_only:
            ctx.actual_work["key_match_component_only"] = True
    except Exception as e:  # noqa: BLE001
        log.warning("key-match stage failed (non-fatal): %s", e)
