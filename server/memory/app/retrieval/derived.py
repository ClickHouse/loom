"""Computed answers over the derived-fact and event planes: cross-session
aggregates over loom.derived_facts, the interval between two event dates,
and the latest value of a single-valued attribute. Each is computed in
ClickHouse so the reader never does the arithmetic.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from server.core.vectors.search_settings import vector_search_settings
from server.memory.app.search_llm import expand_verb_synonyms
from server.memory.domain.search_parsing import _iso_date, expand_predicate_class
from server.memory.domain.search_types import SearchHit

log = logging.getLogger("server.memory.app.retrieval")


# Generic container nouns that carry no category identity on their own.
# Used by the derived-aggregate category matcher: token overlap on these
# alone must NOT bridge two categories ("bike gear" vs "camera gear").
_GENERIC_CATEGORY_TOKENS: frozenset[str] = frozenset(
    {
        "gear",
        "equipment",
        "item",
        "items",
        "stuff",
        "thing",
        "things",
        "accessory",
        "accessories",
        "supplies",
        "supply",
        "product",
        "products",
        "purchase",
        "purchases",
        "expense",
        "expenses",
    }
)


async def fetch_derived_aggregate(
    ch: Any,
    *,
    org: str,
    namespace: str,
    subject: str = "user",
    predicate: str | None = None,
    category: str | None = None,
    object_like: str | None = None,
    subject_canonical_expanded: list[str] | None = None,
    category_canonical_expanded: list[str] | None = None,
    object_canonical_expanded: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    expand_synonyms: bool = True,
    max_items: int = 200,
    on_llm_call: Any = None,
    agent: str = "*",
    user_id: str | None = None,
    session_id: str | None = None,
    session_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """Compute cross-session numeric/temporal aggregates over loom.derived_facts.

    COUNT / SUM / date-diff are computed *in ClickHouse* over per-occurrence
    rows, so the answer is an exact scalar rather than something the reader
    guesses from a recalled passage. Returns::

        {count, sum, avg, first_at, last_at, span_days,
         items: [{object, numeric_value, unit, occurred_at}, ...]}

    `predicate` is widened through `expand_verb_synonyms`. The match key is
    (org, namespace, subject, predicate, category); any of
    subject/predicate/category/object_like/date-window may be omitted.

    `*_canonical_expanded` are the query mention run through
    `lookup_canonical_entities` (self-canonical floor + namespace-local
    aliases); when supplied they replace equality with a canonical `IN (...)`
    so "bike" matches stored "bike gear". The object IN-list fires only when
    the map contributed a REAL alias (len>1) — the self-form floor alone would
    silently narrow the substring match to exact and drop multi-token objects.

    `agent` / `user_id` / `session_id` / `session_ids` bound the rows the way
    the fact leg's `_build_where` bounds its own: `*` / `-` read across
    agents, None reads across users / sessions. A read-scoped caller must pass
    them, or the scalar counts and the items it returns span every user of
    the namespace.

    Returns None on empty match (reader falls back to the vector path); fail-
    open on any error.
    """
    from server.memory.domain.graph_quality import canonical_entity_text
    from server.memory.domain.search_text import canonicalize_verb

    where = ["org = {org:String}", "namespace = {ns:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace}
    if agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id is not None:
        where.append("user_id = {user_id:String}")
        params["user_id"] = user_id
    if session_id is not None:
        where.append("session_id = {session_id:String}")
        params["session_id"] = session_id
    if session_ids:
        where.append("session_id IN {session_ids:Array(String)}")
        params["session_ids"] = [str(x) for x in session_ids]
    # Prefer the canonical IN-list; the scalar fallback is also canonicalized
    # so a direct caller passing only the raw mention still lands on rows.
    if subject_canonical_expanded:
        where.append("subject IN {subjects_expanded:Array(String)}")
        params["subjects_expanded"] = subject_canonical_expanded
    elif subject:
        where.append("subject = {subject:String}")
        params["subject"] = canonical_entity_text(subject) or subject
    if predicate:
        verb = canonicalize_verb(predicate)
        verbs = (
            await expand_verb_synonyms(verb, on_llm_call=on_llm_call) if expand_synonyms else [verb]
        )
        # Deterministic predicate-class floor under the LLM synonym set.
        verb_set = {v for v in verbs if v} | expand_predicate_class(verb)
        where.append("predicate IN {verbs:Array(String)}")
        params["verbs"] = sorted(verb_set)
    if category_canonical_expanded or category:
        # Extraction fragments category surfaces ("camera" / "camera gear"),
        # and exact-matching ONE surface silently drops the rest from the
        # SUM/COUNT — a partial scalar reads as authoritative. Widen with a
        # token-overlap arm on DISTINGUISHING tokens (generic container nouns
        # excluded, so "camera gear" reaches "camera equipment" but never
        # "bike gear"). Exact/IN arm stays first for clean single-word matches.
        _cat_clauses: list[str] = []
        _cat_scalar = canonical_entity_text(category or "") or (category or "")
        if category_canonical_expanded:
            _cat_clauses.append("category IN {categories_expanded:Array(String)}")
            params["categories_expanded"] = category_canonical_expanded
        elif category:
            _cat_clauses.append("category = {category:String}")
            params["category"] = _cat_scalar
        _cat_tokens = [
            t for t in _cat_scalar.split() if len(t) >= 3 and t not in _GENERIC_CATEGORY_TOKENS
        ]
        if _cat_tokens:
            _cat_clauses.append("hasAny(splitByChar(' ', category), {cat_tokens:Array(String)})")
            params["cat_tokens"] = _cat_tokens
        where.append("(" + " OR ".join(_cat_clauses) + ")")
    if object_canonical_expanded and len(object_canonical_expanded) > 1:
        where.append("object IN {objects_expanded:Array(String)}")
        params["objects_expanded"] = object_canonical_expanded
    elif object_like:
        where.append("positionCaseInsensitive(object, {obj:String}) > 0")
        params["obj"] = object_like
    if since is not None:
        where.append("occurred_at IS NOT NULL AND occurred_at >= {since:DateTime}")
        params["since"] = since
    if until is not None:
        where.append("occurred_at IS NOT NULL AND occurred_at <= {until:DateTime}")
        params["until"] = until
    where_sql = " AND ".join(where)

    agg_sql = (
        "SELECT count() AS cnt, "
        "sum(numeric_value) AS s, "
        "avg(numeric_value) AS a, "
        "min(occurred_at) AS first_at, "
        "max(occurred_at) AS last_at "
        "FROM loom.derived_facts WHERE " + where_sql
    )
    items_sql = (
        "SELECT object, numeric_value, unit, occurred_at "
        "FROM loom.derived_facts WHERE " + where_sql + " "
        "ORDER BY occurred_at ASC, object "
        f"LIMIT {int(max_items)}"
    )
    try:
        agg_rows = await ch.query_all(agg_sql, params=params)
        if not agg_rows or int(agg_rows[0].get("cnt") or 0) == 0:
            return None
        agg = agg_rows[0]
        item_rows = await ch.query_all(items_sql, params=params)
    except Exception as e:  # noqa: BLE001 — the aggregate leg is additive; fail open
        log.warning("fetch_derived_aggregate failed (fail-open): %s", e)
        return None

    first_at = agg.get("first_at")
    last_at = agg.get("last_at")
    span_days: float | None = None
    if first_at and last_at:
        try:
            span_days = (last_at - first_at).total_seconds() / 86400.0
        except (TypeError, AttributeError):
            span_days = None
    return {
        "count": int(agg.get("cnt") or 0),
        "sum": (None if agg.get("s") is None else float(agg["s"])),
        "avg": (None if agg.get("a") is None else float(agg["a"])),
        "first_at": first_at,
        "last_at": last_at,
        "span_days": span_days,
        "items": [
            {
                "object": str(r.get("object") or ""),
                "numeric_value": r.get("numeric_value"),
                "unit": str(r.get("unit") or ""),
                "occurred_at": r.get("occurred_at"),
            }
            for r in item_rows
        ],
    }


# Minimum cosine for an operand probe to bind to a dated fact. Below this the
# match is noise: binding it would inject a confidently-wrong computed
# interval, worse than abstaining.
_TEMPORAL_DIFF_MIN_SIMILARITY = 0.35
# An anchored (event-dated) row within this much cosine of the best match
# wins the operand binding over a row dated only by its session.
_TEMPORAL_DIFF_ANCHOR_MARGIN = 0.05


async def _resolve_operand_event_date(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str | None,
    operand_vec: list[float],
    embedding_model: str,
    min_similarity: float = _TEMPORAL_DIFF_MIN_SIMILARITY,
    run_id: str = "",
) -> dict[str, Any] | None:
    """Best dated fact for one operand probe, with its resolved EVENT date.

    ANN over loom.memory_embeddings restricted to rows carrying a date,
    ranked by cosine similarity. The event date is ``coalesce(temporal_anchor,
    valid_at)`` — the resolved in-text event date (temporal_anchor) preferred
    over the session/observation date (valid_at), matching the read-path's
    date precedence. Returns ``{event_date (date), similarity, excerpt}`` or
    ``None`` when nothing dated clears the similarity floor (fail-open).
    """
    # Under a run, the date seed comes from the run's own facts.
    run_sql = "AND run_id = {run_id:String}" if run_id else ""
    sql = f"""
        SELECT toDate(coalesce(temporal_anchor, valid_at)) AS event_date,
               cosineDistance(embedding, {{q:Array(Float32)}}) AS distance,
               substring(content_excerpt, 1, 200) AS excerpt,
               memory_key,
               temporal_anchor IS NOT NULL AS anchored
        FROM loom.memory_embeddings FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND agent = {{agent:String}}
          AND user_id = {{uid:String}}
          AND embedding_model = {{model:String}}
          AND embedding_dim > 0
          AND tombstoned_at IS NULL
          AND status IN ('active', 'superseded', 'conflicted')
          -- TTL: don't let an expired fact seed date arithmetic.
          AND (expires_at IS NULL OR expires_at > now())
          -- Stated validity end: a fact past its own end date
          -- is not the current anchor either.
          AND (valid_to IS NULL OR valid_to > now())
          AND coalesce(temporal_anchor, valid_at) IS NOT NULL
          {run_sql}
        ORDER BY distance ASC
        LIMIT 3
        {vector_search_settings(table="loom.memory_embeddings")}
    """
    try:
        rows = await ch.query_all(
            sql,
            params={
                "q": operand_vec,
                "org": org,
                "ns": namespace,
                "agent": agent,
                **({"run_id": run_id} if run_id else {}),
                "uid": user_id or "-",
                "model": embedding_model,
            },
        )
    except Exception as e:  # noqa: BLE001 — a failed operand read means no date answer
        log.warning("_resolve_operand_event_date failed (fail-open): %s", e)
        return None
    if not rows:
        return None
    # Similarity is reconstructed here, not in SQL: `1.0 - cosineDistance(...)`
    # as the sort key disqualifies the query from CH's vector-search rewrite.
    # Among near-equal matches prefer a row whose date is a
    # resolved EVENT date (temporal_anchor) over one dated only by the session
    # it was said in (valid_at) — in a conversation corpus the mention date can sit weeks
    # after the event and make the interval wrong while looking exact.
    best = rows[0]
    best_sim = 1.0 - float(best.get("distance") or 1.0)
    row = best
    if not bool(best.get("anchored")):
        for cand in rows[1:]:
            cand_sim = 1.0 - float(cand.get("distance") or 1.0)
            if bool(cand.get("anchored")) and cand_sim >= best_sim - _TEMPORAL_DIFF_ANCHOR_MARGIN:
                row = cand
                break
    sim = 1.0 - float(row.get("distance") or 1.0)
    event_date = row.get("event_date")
    if event_date is None or sim < min_similarity:
        return None
    return {
        "event_date": event_date,
        "similarity": sim,
        "excerpt": str(row.get("excerpt") or ""),
        "memory_key": str(row.get("memory_key") or ""),
        "anchored": bool(row.get("anchored")),
    }


async def fetch_current_state_observation(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str | None,
    query_vec: list[float],
    embedding_model: str,
    entity_filter: list[str],
) -> list[SearchHit]:
    """The LATEST-dated value of one single-valued attribute.

    For stated-value-update query shapes the answer is the CURRENT value, not
    the whole history. ANN-restrict to the query's entities, then keep the
    latest-dated row per ``memory_key`` via a window function — read-time and
    non-mutating, so it can't over-delete. Returns at most one
    ``source="observation"`` hit rendered ``current {attr} = {value} (as of
    {date})``.

    Fail-open ``[]`` on any error or when no dated, entity-matching row exists.
    The caller gates this on ``parsed_intent.stated_value_update``; an empty
    ``entity_filter`` also yields ``[]`` (no entity to scope the attribute).
    """
    ents = [e for e in (entity_filter or []) if e]
    if not ents:
        return []
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "user_id = {uid:String}",
        "embedding_model = {model:String}",
        "embedding_dim > 0",
        "tombstoned_at IS NULL",
        "status IN ('active', 'superseded', 'conflicted')",
        # TTL: an expired memory must not resurface as the authoritative
        # "current {attr} = {value}" — the worst place for a lapsed fact.
        "(expires_at IS NULL OR expires_at > now())",
        # Stated validity end: same reasoning as the TTL.
        "(valid_to IS NULL OR valid_to > now())",
        "coalesce(temporal_anchor, valid_at) IS NOT NULL",
        "hasAny(entities, {ents:Array(String)})",
    ]
    params: dict[str, Any] = {
        "q": query_vec,
        "org": org,
        "ns": namespace,
        "uid": user_id or "-",
        "model": embedding_model,
        "ents": ents,
    }
    if agent and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    where_sql = " AND ".join(where)
    sql = f"""
        SELECT memory_key, content_excerpt, event_date, distance FROM (
          SELECT memory_key,
                 content_excerpt,
                 toDate(coalesce(temporal_anchor, valid_at)) AS event_date,
                 cosineDistance(embedding, {{q:Array(Float32)}}) AS distance,
                 row_number() OVER (
                   PARTITION BY memory_key
                   ORDER BY coalesce(temporal_anchor, valid_at) DESC
                 ) AS rn
          FROM loom.memory_embeddings FINAL
          WHERE {where_sql}
        ) WHERE rn = 1
        -- The window function + subquery boundary put this plane permanently
        -- on brute force: the vector-search rewrite needs the LIMIT/sort to sit
        -- directly above the table read. Latest-per-key is the point of the
        -- plane, so that trade stands; ef_search still has to be right.
        ORDER BY distance ASC
        LIMIT 1
        {vector_search_settings(table="loom.memory_embeddings")}
    """
    try:
        rows = await ch.query_all(sql, params=params)
    except Exception as e:  # noqa: BLE001 — the current-state leg is additive; fail open
        log.warning("fetch_current_state_observation failed (fail-open): %s", e)
        return []
    if not rows:
        return []
    row = rows[0]
    event_date = _iso_date(str(row.get("event_date") or ""))
    value = str(row.get("content_excerpt") or "").strip()
    if not value:
        return []
    attr = str(row.get("memory_key") or "").strip() or "value"
    sim = 1.0 - float(row.get("distance") or 1.0)
    as_of = f" (as of {event_date})" if event_date else ""
    excerpt = f"current {attr} = {value}{as_of}"
    return [
        SearchHit(
            memory_key=f"observation:{attr}",
            memory_type="semantic",
            content_excerpt=excerpt[:1200],
            distance=1.0 - sim,
            rank_score=sim,
            temporal_anchor="",
            valid_at=event_date,
            source="observation",
            score_components={"observation_cosine": sim, "rank_score": sim},
        )
    ]


async def fetch_temporal_diff(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str | None,
    operand_vecs: list[list[float]],
    embedding_model: str,
    observation_date: str | None = None,
    to_observation: bool = False,
    run_id: str = "",
) -> dict[str, Any] | None:
    """Deterministic interval between two event dates, computed server-side.

    LLM readers are unreliable at date arithmetic, so the reader must never be
    the one subtracting dates. Binds each operand probe to its best-matching
    dated fact's event date (``coalesce(temporal_anchor, valid_at)``) and
    computes the day interval in ClickHouse with ``dateDiff('day', a, b)``;
    injected as pinned evidence so the reader REPORTS the integer.

    Two shapes:
      * two events ("between A and B", "after I did X when I did Y"):
        ``operand_vecs`` has two probe embeddings; both must bind to a dated
        fact above the similarity floor.
      * one event vs the observation date ("how many days ago did I X"):
        ``to_observation=True``, one probe embedding, second operand is the
        question/observation date.

    Fail-open to ``None`` whenever an operand can't be resolved to a real
    event date — a missing operand must fall back to the normal recall path,
    never inject a guessed interval. LLM-free throughout.
    """
    from datetime import date as _date

    if to_observation:
        if len(operand_vecs) != 1 or not observation_date:
            return None
        try:
            obs = _date.fromisoformat(observation_date.strip()[:10])
        except (ValueError, AttributeError):
            return None
    elif len(operand_vecs) != 2:
        return None

    resolved = await asyncio.gather(
        *[
            _resolve_operand_event_date(
                ch,
                org=org,
                namespace=namespace,
                agent=agent,
                user_id=user_id,
                operand_vec=v,
                embedding_model=embedding_model,
                run_id=run_id,
            )
            for v in operand_vecs
        ]
    )
    found = [r for r in resolved if r is not None]
    if len(found) != len(resolved):
        return None
    # Two probes that bind to the SAME fact are one event, not
    # an interval — "0 days" from a single row would override a reader that
    # rightly abstains. Fail-open instead.
    if len(found) == 2:
        k0, k1 = found[0].get("memory_key") or "", found[1].get("memory_key") or ""
        same_fact = (
            (k0 == k1) if (k0 and k1) else (found[0].get("excerpt") == found[1].get("excerpt"))
        )
        if same_fact:
            return None

    a_date = found[0]["event_date"]
    a_excerpt = found[0]["excerpt"]
    if to_observation:
        b_date = obs
        b_excerpt = f"observation date {obs.isoformat()}"
    else:
        b_date = found[1]["event_date"]
        b_excerpt = found[1]["excerpt"]

    # Order the pair so the reported interval is the non-negative magnitude
    # ("days between" is direction-free).
    a_anchored = bool(found[0].get("anchored", True))
    b_anchored = bool(found[1].get("anchored", True)) if not to_observation else True
    earlier, later = (a_date, b_date) if a_date <= b_date else (b_date, a_date)
    earlier_excerpt, later_excerpt = (
        (a_excerpt, b_excerpt) if a_date <= b_date else (b_excerpt, a_excerpt)
    )
    earlier_anchored, later_anchored = (
        (a_anchored, b_anchored) if a_date <= b_date else (b_anchored, a_anchored)
    )
    try:
        rows = await ch.query_all(
            "SELECT dateDiff('day', {a:Date}, {b:Date}) AS days",
            params={"a": earlier, "b": later},
        )
    except Exception as e:  # noqa: BLE001 — a failed date diff means no temporal answer
        log.warning("fetch_temporal_diff failed (fail-open): %s", e)
        return None
    if not rows:
        return None
    interval_days = int(rows[0].get("days") or 0)
    return {
        "interval_days": interval_days,
        "from_date": earlier.isoformat(),
        "to_date": later.isoformat(),
        "from_excerpt": earlier_excerpt,
        "to_excerpt": later_excerpt,
        "to_observation": bool(to_observation),
        # A date that is the session a fact was mentioned in
        # (valid_at, no temporal_anchor) is at or after the event. A
        # mention-dated START makes the interval a LOWER bound, a
        # mention-dated END an UPPER bound; the evidence line says which.
        "from_event_dated": bool(earlier_anchored),
        "to_event_dated": bool(later_anchored),
        "all_event_dated": bool(earlier_anchored and later_anchored),
    }
