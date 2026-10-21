"""Summary legs: session summaries and entity summaries matched to the
query, the as-of predicate they share, and the per-process cache of which
entities have a summary at all.
"""

from __future__ import annotations

import logging
from typing import Any

from server.core.vectors.search_settings import vector_search_settings
from server.memory.domain.search_parsing import _iso_date
from server.memory.domain.search_types import SearchHit
from server.memory.domain.sensitivity import without_credentials
from server.settings import settings

log = logging.getLogger("server.memory.app.retrieval")


# Top-N session summaries by cosine. Module constant, not a wire knob — the
# assembler re-balances planes via RRF so a larger N never floods the block.
_SESSION_SUMMARY_TOP_N = 5
# Characters of a digest a hit shows; the whole text is read, so it can be checked first.
_SUMMARY_EXCERPT_CHARS = 1200  # not a setting: wire bound on a hit's excerpt

# Under agent='*' the digest leg fetches this many times its N and dedups per
# session in Python: a session can carry one row per agent,
# and the statement carries no `LIMIT 1 BY session_id` so the codes-first
# shortlist and the lazy column read apply. A namespace with more than four
# agents per session in the top rows would serve fewer than N digests under
# the wildcard.
_SESSION_SUMMARY_OVERFETCH = 4


def dedup_summary_rows(rows: list[dict[str, Any]], *, n: int) -> list[dict[str, Any]]:
    """The first `n` rows with distinct `session_id` in `(distance, session_id)`
    order — what `ORDER BY distance ASC, session_id ASC LIMIT 1 BY session_id
    LIMIT n` would return. The statement orders by the bare distance (the
    shortlist's shape) and over-fetches; the deterministic tiebreak lives
    here."""

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    ordered = sorted(
        rows, key=lambda r: (float(r.get("distance") or 1.0), str(r.get("session_id") or ""))
    )
    for r in ordered:
        sid = str(r.get("session_id") or "")
        if sid in seen:
            continue
        seen.add(sid)
        out.append(r)
        if len(out) >= n:
            break
    return out


# Pre-built per-entity summaries, top-N by cosine. Module-constant fallback;
# the handler threads settings.memory_search_entity_summaries_top_n.
_ENTITY_SUMMARY_TOP_N = 5


def _as_of_clause(
    as_of: Any,
    as_of_basis: str,
    *,
    valid_col: str,
    created_col: str,
    params: dict[str, Any],
    anchor_col: str | None = None,
    invalid_col: str | None = None,
    tomb_col: str | None = None,
) -> str | None:
    """WHERE fragment that replays a summary/observation leg to `as_of`, so it
    honours the same temporal cutoff the vector leg gets from search_sql.
    Returns None (and touches nothing) when `as_of` is unset. Built by
    server/temporal_sql so every leg shares one definition of both axes;
    pass `anchor_col` when the table carries an event date, and
    `invalid_col`/`tomb_col` so knowledge_time keeps rows retired after T."""
    if as_of is None:
        return None
    params["as_of"] = as_of
    from server.memory.domain.temporal_sql import as_of_predicate

    return as_of_predicate(
        as_of_basis,
        valid_col=valid_col,
        created_col=created_col,
        anchor_col=anchor_col,
        invalid_col=invalid_col,
        tomb_col=tomb_col,
    )


async def fetch_session_summaries(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str | None,
    query_vec: list[float],
    embedding_model: str,
    n: int = _SESSION_SUMMARY_TOP_N,
    as_of: Any = None,
    as_of_basis: str = "valid_time",
    run_id: str | None = None,
    session_id: str | None = None,
    session_ids: list[str] | None = None,
) -> list[SearchHit]:
    """Top-N session summaries by cosine.

    `run_id`: when set, only digests written under that run
    are eligible — the same predicate the fact and chunk legs carry, so a
    run-scoped read does not fold other runs' digests into the pool.
    `session_id` / `session_ids` are the read's session bound (an own_session
    read scope, the caller's session pick), applied as the fact leg applies
    them; None / empty = every session.

    Reads `loom.session_summaries` (one RMT(version) row per session, embedded
    with the same model/dim as facts so the cosine is comparable). Returns
    ``source="summary"`` hits that compete in RRF — they do NOT bypass fusion
    as pinned rows.

    Agent scope mirrors `_build_where`: an explicit agent (not ``"*"``) filters
    on it; ``"*"`` drops the predicate so cross-agent summaries are visible.

    Fail-open ``[]`` on any error — the summary plane is additive.
    """
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "user_id = {uid:String}",
        "embedding_model = {model:String}",
        "embedding_dim > 0",
        "tombstoned_at IS NULL",
    ]
    params: dict[str, Any] = {
        "q": query_vec,
        "org": org,
        "ns": namespace,
        "uid": user_id or "-",
        "model": embedding_model,
    }
    if agent and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if run_id is not None:
        where.append("run_id = {run_id:String}")
        params["run_id"] = run_id
    if session_id is not None:
        where.append("session_id = {session_id:String}")
        params["session_id"] = session_id
    if session_ids:
        where.append("session_id IN {session_ids:Array(String)}")
        params["session_ids"] = [str(x) for x in session_ids]
    _aoc = _as_of_clause(
        as_of, as_of_basis, valid_col="valid_at", created_col="created_at", params=params
    )
    if _aoc:
        where.append(_aoc)
    where_sql = " AND ".join(where)
    sql = f"""
        SELECT session_id,
               summary_text,
               cosineDistance(summary_embedding, {{q:Array(Float32)}}) AS distance,
               -- NB: alias must NOT be `valid_at` — under the analyzer a
               -- SELECT alias shadows the base column, so the as_of WHERE clause
               -- (`valid_at <= {{as_of:DateTime}}`) would bind to this String
               -- expression instead of the DateTime column and raise "no
               -- supertype for String, DateTime" — silently swallowed by the
               -- except below, killing the whole leg under any as_of.
               toString(coalesce(valid_at, toDateTime(0))) AS valid_at_iso,
               derived_from
        FROM loom.session_summaries FINAL
        WHERE {where_sql}
        -- No `LIMIT 1 BY` and no `session_id` tiebreak here:
        -- a LimitByStep between the sort and the LIMIT blocks the lazy column
        -- read, and anything but the bare distance in ORDER BY blocks the
        -- codes-first shortlist (the plan-shape note in search_sql), so the
        -- statement would read every digest's vector and text. The
        -- per-session dedup is load-bearing — with agent='*' one session can
        -- carry a row per agent — and the (distance, session_id) order is the
        -- served order; both happen in Python below over an over-fetched
        -- shortlist.
        ORDER BY distance ASC
        LIMIT {{lim:UInt32}}
        {vector_search_settings(table="loom.session_summaries", column="summary_embedding")}
    """
    # With an explicit agent the table key (org, namespace, agent, user_id,
    # session_id, embedding_model) is one row per session under FINAL, so
    # there is nothing to dedup and the shortlist is exactly n rows. Only the
    # wildcard can fold several agents' digests of one session — over-fetch
    # there. The shortlist's exact re-read is per granule, so a larger LIMIT
    # costs whole granules of vectors: measured 7.2 MiB at 5 rows, 13.3 MiB at
    # 20 on a 1,564-row namespace.
    wildcard = not agent or agent in ("*", "-")
    params["lim"] = int(n) * (_SESSION_SUMMARY_OVERFETCH if wildcard else 1)
    try:
        rows = await ch.query_all(sql, params=params)
    except Exception as e:  # noqa: BLE001 — the session-summary leg is additive; fail open
        log.warning("fetch_session_summaries failed (fail-open): %s", e)
        return []
    # A digest that reads as a credential is written without a vector; this holds back
    # one an earlier build embedded. No spare row backfills it: such a digest is rare,
    # and a spare row costs a granule of vectors on every search.
    rows = without_credentials(rows, lambda r: str(r.get("summary_text") or ""))
    rows = dedup_summary_rows(rows, n=int(n))
    out: list[SearchHit] = []
    for r in rows:
        session_id = str(r.get("session_id") or "")
        distance = float(r.get("distance") or 1.0)
        sim = 1.0 - distance
        out.append(
            SearchHit(
                memory_key=f"summary:{session_id}",
                memory_type="semantic",
                content_excerpt=str(r.get("summary_text") or "")[:_SUMMARY_EXCERPT_CHARS],
                distance=distance,
                rank_score=sim,
                temporal_anchor="",
                # valid_at is a per-line label only, never a sort key.
                valid_at=_iso_date(str(r.get("valid_at_iso") or "")),
                source="summary",
                # The digest names its session and the keys it compressed.
                session_id=session_id,
                derived_from=[str(k) for k in (r.get("derived_from") or []) if str(k)],
                score_components={"summary_cosine": sim, "rank_score": sim},
            )
        )
    return out


# The entity-summary plane doubles as the vocabulary of "entities we hold a
# consolidation for". A short-TTL cache of that vocabulary lets the read leg
# match a query's word-tokens against real summary entities (bare user names,
# not only @handles) without an LLM enrichment call or a
# per-query round trip. Keyed by (org, sorted namespaces); values are lowercased
# entity strings. Populated only while the read leg is enabled.
_SUMMARY_ENTITY_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, frozenset[str]]] = {}


async def summary_entities_present(ch: Any, org: str, namespaces: list[str]) -> frozenset[str]:
    """Lowercased set of entities that have a live summary row in `namespaces`.

    Cached per (org, namespaces) for a short TTL — the summary plane changes
    only when the offline worker runs, so brief staleness is fine and the hot
    path pays at most one small DISTINCT scan per minute. Fail-open to the empty
    set (the leg simply falls back to its @handle/enrichment seeds)."""
    import time

    key = (org, tuple(sorted(set(namespaces))))
    now = time.monotonic()
    cached = _SUMMARY_ENTITY_CACHE.get(key)
    if cached is not None and (now - cached[0]) < settings.memory_summary_entity_ttl_s:
        return cached[1]
    try:
        rows = await ch.query_all(
            """
            SELECT DISTINCT lower(entity) AS e
            FROM loom.entity_summaries FINAL
            WHERE org = {org:String}
              AND namespace IN {ns:Array(String)}
              AND tombstoned_at IS NULL
              AND embedding_dim > 0
            """,
            params={"org": org, "ns": sorted(set(namespaces))},
        )
        ents = frozenset(str(r.get("e") or "") for r in rows if r.get("e"))
    except Exception as e:  # noqa: BLE001 — a failed vocabulary read disables the summary gate
        log.warning("summary_entities_present failed (fail-open): %s", e)
        ents = frozenset()
    _SUMMARY_ENTITY_CACHE[key] = (now, ents)
    return ents


async def fetch_entity_summaries(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str | None,
    query_vec: list[float],
    embedding_model: str,
    entity_filter: list[str],
    n: int = _ENTITY_SUMMARY_TOP_N,
    as_of: Any = None,
    as_of_basis: str = "valid_time",
) -> list[SearchHit]:
    """Top-N pre-built per-entity summaries for the query's entities, by cosine.

    Reads the worker-owned `loom.entity_summaries` plane (one RMT(version) row
    per canonical entity, embedded with the same model/dim as facts so the
    cosine is directly comparable). Returns ``source="entity_summary"`` hits
    that compete in RRF as additive dense rows — they do NOT bypass fusion or
    displace pool hits. Pure CH read of pre-built rows, no LLM on the hot path.

    Scoped to the query's entities (``entity_filter``, already canonical/alias-
    expanded by the caller); an empty filter yields ``[]``. Agent scope mirrors
    the shared-vs-private convention: an explicit agent (anything but ``"*"``)
    filters on it. Fail-open ``[]`` on any error — additive, never critical-path.
    """
    ents = sorted({e for e in (entity_filter or []) if e})
    if not ents:
        return []
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "user_id = {uid:String}",
        "embedding_model = {model:String}",
        "embedding_dim > 0",
        "tombstoned_at IS NULL",
        "entity IN {ents:Array(String)}",
    ]
    params: dict[str, Any] = {
        "q": query_vec,
        "org": org,
        "ns": namespace,
        "uid": user_id or "-",
        "model": embedding_model,
        "ents": ents,
        "n": int(n),
    }
    if agent and agent not in ("*", "-"):
        # A named agent reads its own summaries plus the shared ones: rows
        # written with agent '*' are shared by construction (the community
        # summaries are the only writer of that marker).
        where.append("agent IN ({agent:String}, '*')")
        params["agent"] = agent
    _aoc = _as_of_clause(
        as_of, as_of_basis, valid_col="valid_from", created_col="created_at", params=params
    )
    if _aoc:
        where.append(_aoc)
    where_sql = " AND ".join(where)
    sql = f"""
        SELECT entity,
               substring(summary_text, 1, 1200) AS content_excerpt,
               cosineDistance(summary_embedding, {{q:Array(Float32)}}) AS distance,
               -- alias must NOT be `valid_from`: it would shadow the base column
               -- in the as_of WHERE clause and raise a String/DateTime supertype
               -- error that the except below swallows, silently killing the leg
               -- whenever as_of is set. See fetch_session_summaries.
               toString(coalesce(valid_from, toDateTime(0))) AS valid_from_iso
        FROM loom.entity_summaries FINAL
        WHERE {where_sql}
        -- `LIMIT 1 BY entity` blocks the vector-search rewrite, same as the
        -- session-summary plane above; the per-entity dedup is worth it here.
        ORDER BY distance ASC, entity ASC
        LIMIT 1 BY entity
        LIMIT {{n:UInt32}}
        {vector_search_settings(table="loom.entity_summaries", column="summary_embedding")}
    """
    try:
        rows = await ch.query_all(sql, params=params)
    except Exception as e:  # noqa: BLE001 — the entity-summary leg is additive; fail open
        log.warning("fetch_entity_summaries failed (fail-open): %s", e)
        return []
    out: list[SearchHit] = []
    for r in rows:
        entity = str(r.get("entity") or "")
        distance = float(r.get("distance") or 1.0)
        sim = 1.0 - distance
        out.append(
            SearchHit(
                memory_key=f"entity_summary:{entity}",
                memory_type="semantic",
                content_excerpt=str(r.get("content_excerpt") or ""),
                distance=distance,
                rank_score=sim,
                temporal_anchor="",
                # valid_from is a per-line LABEL only, never a sort key. The
                # "1970-01-01" sentinel (coalesce default) reads as undated.
                valid_at=_iso_date(str(r.get("valid_from_iso") or "")),
                source="entity_summary",
                score_components={"entity_summary_cosine": sim, "rank_score": sim},
            )
        )
    return out
