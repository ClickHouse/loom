"""The fact leg's rank terms: the SQL fragments one read ranks by (entity,
tag, category, intent, personalization and correctness terms), the
per-mode rank expression and the capped prior group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.memory.app.retrieval.lexical import _lexical_score_expr, bm25_lexical_expr
from server.memory.domain.search_types import SearchQuery
from server.memory.domain.temporal_confidence import anchor_weight_sql


def _prior_group_expr(
    tag_boost_expr: str,
    category_boost_expr: str,
    prior_cap: float,
) -> tuple[str, bool]:
    """Discretionary-prior SQL group (stored confidence + soft tag/category)
    and whether a {prior_cap} param is needed.

    prior_cap <= 0 -> raw additive group, uncapped. prior_cap > 0 -> the
    group is bounded by least({prior_cap}, group) so it only reorders
    near-ties, never outranks a materially more relevant memory.
    """
    group = (
        "{confidence_weight:Float32} * e.confidence "
        f"+ ({tag_boost_expr}) + ({category_boost_expr})"
    )
    if prior_cap and prior_cap > 0:
        return f"least({{prior_cap:Float32}}, {group})", True
    return group, False


@dataclass(slots=True)
class _FactRankTerms:
    """The SQL fragments one fact-leg read ranks by, built across the rank
    phases of `vector_search` and spliced into its SELECT. Request-scoped."""

    entity_boost_expr: str
    tag_boost_expr: str
    category_boost_expr: str
    demote_expr: str
    conflicted_demote_expr: str
    sensitivity_demote_expr: str
    reinforcement_boost_expr: str
    lexical_score_expr: str
    recency_boost_expr: str
    chronological_boost_expr: str
    temporal_anchor_boost_expr: str
    preference_boost_expr: str
    # Personalization and bare ranking.
    no_priors: bool = False
    eligible: Any = None
    trust_boost_expr: str = "0.0"
    # The per-mode rank expression, then the tail's prior group.
    rank_score_expr: str = ""
    prior_group_select: str = "0.0"


def _fact_intent_boosts(q: SearchQuery, params: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Phase 2, second half: the lexical arm and the query-intent boosts
    (recency, chronological order, temporal anchor, preference)."""
    # Lexical arm: one expression, spelled once.
    lexical_score_expr = _lexical_score_expr(q, params)

    # Recency boost on a recency-intent cue: 0.25 / (1 + age_years) — tips ties
    # toward the newest version without overriding strong semantic similarity.
    if q.recency_intent:
        recency_boost_expr = (
            "0.25 / (1.0 + toFloat32(dateDiff('day', "
            "coalesce(e.valid_at, e.created_at), now())) / 365.0)"
        )
    else:
        recency_boost_expr = "0.0"

    # Chronological-order boost, capped at 0.3 (discriminates similar-cosine
    # candidates by date without letting age alone outrank relevance).
    if q.chronological_intent == "asc":
        chronological_boost_expr = (
            "0.3 * least(1.0, toFloat32(dateDiff('day', "
            "coalesce(e.valid_at, e.created_at), now())) / 1825.0)"
        )
    elif q.chronological_intent == "desc":
        chronological_boost_expr = (
            "0.3 / (1.0 + toFloat32(dateDiff('day', "
            "coalesce(e.valid_at, e.created_at), now())) / 365.0)"
        )
    else:
        chronological_boost_expr = "0.0"

    # Temporal-anchor boost: enrichment resolves a date and we boost memories
    # whose temporal_anchor falls within ±anchor_window_days. Magnitude 0.4 —
    # a date match is a definitive signal for temporal-reasoning queries —
    # scaled by how precisely the source pinned the date (temporal_confidence): a fact anchored from "a while back" earns 0.35 of the
    # boost, an unscored row (0) keeps all of it.
    if q.query_anchor_iso:
        params["query_anchor"] = q.query_anchor_iso
        params["anchor_window"] = int(q.anchor_window_days)
        temporal_anchor_boost_expr = (
            "if(e.temporal_anchor IS NOT NULL "
            "AND abs(dateDiff('day', e.temporal_anchor, "
            "parseDateTimeBestEffortOrNull({query_anchor:String}))) "
            "<= {anchor_window:UInt32}, 0.4 * "
            + anchor_weight_sql("e.temporal_confidence")
            + ", 0.0)"
        )
    else:
        temporal_anchor_boost_expr = "0.0"

    # Preference boost: 0.3 for an exact `preference:<attr>` tag match, else a
    # weaker 0.15 fallback for any preference-typed/tagged memory. Additive,
    # never a hard filter — preference facts stored as plain semantic stay
    # retrievable, they just don't get the bonus.
    # The fallback magnitude is a deployment setting: 0.15 is
    # the default; 0.0 makes the boost intent-gated — only a query
    # that names the attribute earns it.
    from server.settings import settings as _pref_settings

    params["pref_fallback"] = float(_pref_settings.memory_search_preference_fallback)
    pref_fallback = (
        "if(e.memory_type = 'preference' OR has(e.tags, 'preference'), "
        "{pref_fallback:Float32}, 0.0)"
    )
    if q.preference_attribute:
        params["pref_tag"] = "preference:" + q.preference_attribute
        preference_boost_expr = f"if(has(e.tags, {{pref_tag:String}}), 0.3, {pref_fallback})"
    else:
        preference_boost_expr = pref_fallback
    return (
        lexical_score_expr,
        recency_boost_expr,
        chronological_boost_expr,
        temporal_anchor_boost_expr,
        preference_boost_expr,
    )


def _fact_boost_terms(q: SearchQuery, params: dict[str, Any]) -> _FactRankTerms:
    """Phase 2 of `vector_search`: every additive boost / demotion term, as
    SQL, with its parameters bound into `params`."""
    # Entity-filter boost: fractional overlap (3-of-3 ranks above 1-of-3),
    # capped at 0.5.
    entity_boost_expr = "0.0"
    if q.entity_filter:
        _raw_ents = [t.lower() for t in q.entity_filter]
        entity_boost_expr = (
            "0.5 * length(arrayIntersect(e.entities, {ents:Array(String)})) "
            "/ greatest(toFloat32(length({ents:Array(String)})), 1.0)"
        )
        params["ents"] = _raw_ents

    # Soft-metadata boosts (mirror entity_boost): when the caller opts into
    # tags_soft / category_soft, tag / category overlap becomes an additive rank
    # term instead of a hard WHERE filter — sharpen without losing coverage.
    tag_boost_expr = "0.0"
    if q.tags and q.tags_soft:
        tag_boost_expr = (
            "{tag_boost:Float32} "
            "* length(arrayIntersect(e.tags, {soft_tags:Array(String)})) "
            "/ greatest(toFloat32(length({soft_tags:Array(String)})), 1.0)"
        )
        params["soft_tags"] = list(q.tags)
        params["tag_boost"] = float(q.tag_boost)

    category_boost_expr = "0.0"
    if q.category and q.category_soft:
        # Members of the named category get a flat +category_boost.
        category_boost_expr = (
            "if(e.memory_key IN ("
            "  SELECT memory_key FROM loom.memory_categories FINAL"
            "  WHERE org = {org:String} AND namespace = {ns:String}"
            "    AND tombstoned_at IS NULL AND category_id = ("
            "      SELECT category_id FROM loom.categories FINAL"
            "      WHERE org = {org:String} AND namespace = {ns:String}"
            "        AND name = {category:String} AND tombstoned_at IS NULL LIMIT 1"
            "    )"
            "), {category_boost:Float32}, 0.0)"
        )
        params["category"] = q.category
        params["category_boost"] = float(q.category_boost)

    # Hard-demote: rows whose tags intersect demote_tags sink below everything
    # (large negative term) but are NOT dropped — for known-noise markers.
    demote_expr = "0.0"
    # Conflicted-status demotion: a status='conflicted' row still surfaces
    # but sinks below clean matches by `conflicted_penalty` (0.0 = none; not
    # the -1000 hard sink). Conflicted means "has a close peer", not wrong.
    conflicted_demote_expr = "0.0"
    if q.conflicted_penalty and q.conflicted_penalty > 0:
        conflicted_demote_expr = (
            "if(e.status = 'conflicted', -1.0 * {conflicted_penalty:Float32}, 0.0)"
        )
        params["conflicted_penalty"] = float(q.conflicted_penalty)
    if q.demote_tags:
        demote_expr = "if(hasAny(e.tags, {demote_tags:Array(String)}), -1000.0, 0.0)"
        params["demote_tags"] = list(q.demote_tags)
    # Sensitivity demotion: a labelled contact detail sinks a
    # little below unlabelled matches unless the question asked for it (the
    # handler zeroes the penalty on contact intent). Same correctness tail as
    # the conflicted demotion; survives bare ranking.
    sensitivity_demote_expr = "0.0"
    if q.sensitivity_penalty > 0:
        sensitivity_demote_expr = (
            "if(hasAny(e.tags, ['sensitivity:medium', 'sensitivity:high']), "
            "-1.0 * {sensitivity_penalty:Float32}, 0.0)"
        )
        params["sensitivity_penalty"] = float(q.sensitivity_penalty)

    # Decayed-reinforcement boost: reinforcement_count (folded from recall +
    # positive memory.outcome by usage_fold) log-scaled and decayed by time
    # since last reinforcement. Makes outcome feed ranking and gives efficacy
    # decay — un-reinforced memories fade. Capped so it can't outrank relevance.
    reinforcement_boost_expr = "0.0"
    if q.reinforcement_weight and q.reinforcement_weight > 0:
        # Half-life: fixed, or growing with use —
        # half_life · (1 + scale · log1p(count − 1)); scale 0 emits the
        # fixed form byte-for-byte.
        use_scale = float(q.reinforcement_half_life_use_scale)
        halflife_expr = (
            "({reinf_halflife:Float32} * (1.0 + {reinf_use_scale:Float32} "
            "* log(1.0 + greatest(toFloat32(e.reinforcement_count) - 1.0, 0.0))))"
            if use_scale > 0
            else "{reinf_halflife:Float32}"
        )
        reinforcement_boost_expr = (
            "least({reinf_cap:Float32}, {reinf_weight:Float32} "
            "* log(1.0 + greatest(toFloat32(e.reinforcement_count) - 1.0, 0.0)) "
            "* exp(-1.0 * toFloat32(dateDiff('day', "
            "coalesce(e.last_reinforced_at, e.created_at), now())) "
            f"/ greatest({halflife_expr}, 1.0)))"
        )
        params["reinf_weight"] = float(q.reinforcement_weight)
        params["reinf_halflife"] = float(q.reinforcement_half_life_days)
        params["reinf_cap"] = 0.5
        if use_scale > 0:
            params["reinf_use_scale"] = use_scale

    (
        lexical_score_expr,
        recency_boost_expr,
        chronological_boost_expr,
        temporal_anchor_boost_expr,
        preference_boost_expr,
    ) = _fact_intent_boosts(q, params)

    return _FactRankTerms(
        entity_boost_expr=entity_boost_expr,
        tag_boost_expr=tag_boost_expr,
        category_boost_expr=category_boost_expr,
        demote_expr=demote_expr,
        conflicted_demote_expr=conflicted_demote_expr,
        sensitivity_demote_expr=sensitivity_demote_expr,
        reinforcement_boost_expr=reinforcement_boost_expr,
        lexical_score_expr=lexical_score_expr,
        recency_boost_expr=recency_boost_expr,
        chronological_boost_expr=chronological_boost_expr,
        temporal_anchor_boost_expr=temporal_anchor_boost_expr,
        preference_boost_expr=preference_boost_expr,
    )


def _personalize_fact_terms(q: SearchQuery, params: dict[str, Any], t: _FactRankTerms) -> None:
    """Phase 3 of `vector_search`: gate the user-derived terms on the
    personalization arm and provenance, add the trust term, and decide bare
    ranking. Mutates `t`; binds its parameters into `params`."""
    # Personalization arm + provenance eligibility. "none" (and semantic
    # mode, which ranks bare) zeroes every user-derived term below; the
    # eligibility set wraps each of them in `has(eligible, e.source_class)` so
    # an extracted or LLM-derived row is ranked on relevance alone while a
    # user-asserted one keeps its priors. Both are per request.
    no_priors = q.personalization == "none" or q.search_mode == "semantic"
    _eligible = q.source_class_boost_eligible
    if _eligible is not None:
        params["sc_eligible"] = [str(x) for x in _eligible]

    def _user_derived(expr: str) -> str:
        if no_priors or expr == "0.0":
            return "0.0"
        if _eligible is not None:
            return f"if(has({{sc_eligible:Array(String)}}, e.source_class), {expr}, 0.0)"
        return expr

    t.preference_boost_expr = _user_derived(t.preference_boost_expr)
    t.reinforcement_boost_expr = _user_derived(t.reinforcement_boost_expr)
    t.tag_boost_expr = _user_derived(t.tag_boost_expr)
    t.category_boost_expr = _user_derived(t.category_boost_expr)
    t.prior_group_select = "0.0"

    # Trust-aware term: provenance class + attestation, gated
    # by memory_search_trust_weight; "0.0" at the default so nothing moves.
    from server.memory.domain.probation import trust_boost_sql as _trust_boost_sql
    from server.settings import settings as _trust_settings

    trust_boost_expr = _trust_boost_sql(float(_trust_settings.memory_search_trust_weight), params)
    t.no_priors = no_priors
    t.eligible = _eligible
    t.trust_boost_expr = trust_boost_expr


def _bm25_hybrid_rank_score(
    q: SearchQuery, where: list[str], params: dict[str, Any], t: _FactRankTerms
) -> str:
    """The `bm25_hybrid` arm of phase 4: BM25 (or its cold-namespace token
    overlap) alpha-blended with cosine, plus its token-or-neighbour WHERE."""
    # IDF-weighted Okapi BM25 fused with cosine. The precursor above
    # populated bm25_idfs / bm25_avgdl from the term-freq MVs; a cold
    # namespace falls back to a token-overlap fraction. The hasAnyTokens
    # predicate below activates the text index when it exists.
    from server.memory.domain.lemmatize import (
        lemma_tokens,
        lemmagen_lemma_tokens,
        snowball_stem_tokens,
    )

    tokens = list(q.bm25_tokens) if q.bm25_tokens else lemma_tokens(q.query_text)
    params["query_tokens"] = tokens or [""]  # CH dislikes empty arrays
    params["query_stem_tokens"] = snowball_stem_tokens(tokens) or [""]
    params["query_lemma_tokens"] = lemmagen_lemma_tokens(tokens) or [""]
    params["alpha"] = float(q.alpha)

    idfs = list(q.bm25_idfs) if q.bm25_idfs else []
    if idfs and len(idfs) == len(tokens) and q.bm25_avgdl > 0:
        lexical_expr = bm25_lexical_expr(q, params, tokens)
    else:
        # Cold-namespace fallback: token-overlap fraction.
        lexical_expr = (
            "arraySum(arrayMap("
            "  t -> if(positionCaseInsensitive(e.content_excerpt, t) > 0, 1.0, 0.0), "
            "  {query_tokens:Array(String)})) "
            "/ greatest(toFloat32(length({query_tokens:Array(String)})), 1.0)"
        )

    rank_score_expr = (
        f"{{alpha:Float32}} * ({lexical_expr}) + "
        f"(1.0 - {{alpha:Float32}}) * (1.0 - distance) + "
        f"{t.entity_boost_expr} + "
        f"{t.recency_boost_expr} + "
        f"{t.chronological_boost_expr} + "
        f"{t.preference_boost_expr}"
    )
    # Match on any query token OR a strong semantic neighbour (distance
    # < 0.6); the OR keeps semantic-only hits the hybrid score should
    # still surface.
    where.append(
        "(hasAnyTokens(e.content_excerpt_lc, {query_tokens:Array(String)}) "
        "OR hasAnyTokens(e.content_excerpt_stem, {query_stem_tokens:Array(String)}) "
        "OR hasAnyTokens(e.content_excerpt_lemma, {query_lemma_tokens:Array(String)}) "
        "OR cosineDistance(e.embedding, {q:Array(Float32)}) < 0.6)"
    )
    return rank_score_expr


def _graph_rank_score(q: SearchQuery, params: dict[str, Any], t: _FactRankTerms) -> str:
    """The `graph` arm of phase 4: cosine plus the capped structural group
    (query entities, 1-hop overlap, triple match)."""
    # Boost memories whose entities overlap the 1-hop-expanded query
    # entity set. The expansion is fetched by the handler and passed in
    # as `graph_expanded`, so this branch stays cheap (no edge JOIN on
    # the hot ANN path); cosine-ordered for HNSW, boost is additive.
    params["graph_entities"] = list(q.graph_expanded or [])
    params["triple_keys"] = list(q.triple_matched_keys or [])
    # Weights default to 0.3 / 0.2.
    params["graph_overlap_w"] = float(q.graph_overlap_weight)
    params["graph_triple_w"] = float(q.graph_triple_weight)
    graph_overlap_expr = (
        "if(length({graph_entities:Array(String)}) > 0, "
        "{graph_overlap_w:Float32} * length(arrayIntersect(e.entities, "
        "{graph_entities:Array(String)})) "
        "/ greatest(length(e.entities), 1), 0.0)"
    )
    # Triple-match boost, kept small: triple extraction fires on a minority
    # of rows, so over-weighting that subset displaces candidates.
    triple_boost_expr = (
        "if(has({triple_keys:Array(String)}, memory_key), {graph_triple_w:Float32}, 0.0)"
    )
    # The structural group rides under the same cap as the discretionary
    # priors. Uncapped, a row whose every entity sits in the 1-hop expansion
    # earns the full overlap weight (0.3, the width of the usable cosine band)
    # and displaces the direct match. Capped, structure reorders near-ties and
    # cannot outrank a materially closer memory; prior_cap <= 0 keeps the
    # additive form.
    # The query-entity boost (0.5 × overlap with the question's own entities)
    # is the largest term of all and belongs in the same group: uncapped, every
    # row naming a question entity outranks the entity-free session chunks
    # that often hold the answer.
    structural_group = f"({t.entity_boost_expr} + {graph_overlap_expr} + {triple_boost_expr})"
    if q.prior_cap and q.prior_cap > 0:
        params.setdefault("prior_cap", float(q.prior_cap))
        structural_group = f"least({{prior_cap:Float32}}, {structural_group})"
    # Graph ranks bare like semantic: cosine, the capped structural group and
    # the correctness tail; no recency / preference / chronological boost.
    # The temporal-anchor boost stays: it is graded by temporal_confidence and
    # fires only when the question resolved a date — a correctness term, not a
    # prior.
    return f"(1.0 - distance) + {structural_group} + {t.temporal_anchor_boost_expr}"


def _fact_mode_rank_score(
    q: SearchQuery, where: list[str], params: dict[str, Any], t: _FactRankTerms
) -> None:
    """Phase 4 of `vector_search`: the per-mode rank expression (and any
    mode-specific WHERE predicate). Sets `t.rank_score_expr`."""
    if q.search_mode == "lexical":
        # Match rows where any token appears in content OR key. The lc/stem/
        # lemma legs use hasAnyTokens (no text index; the key prefix prunes); the key leg
        # uses positionCaseInsensitive (short, ngrambf-indexed). Stem + lemma
        # legs catch morphological variants the raw lc leg misses.
        where.append(
            "(hasAnyTokens(e.content_excerpt_lc, {score_tokens:Array(String)}) "
            " OR hasAnyTokens(e.content_excerpt_stem, {score_stem_tokens:Array(String)}) "
            " OR hasAnyTokens(e.content_excerpt_lemma, {score_lemma_tokens:Array(String)}) "
            " OR arrayExists(t -> positionCaseInsensitive(e.memory_key, t) > 0, "
            "                {score_tokens:Array(String)}) "
            " OR positionCaseInsensitive(replaceAll(e.memory_key, '_', ' '), "
            "                            {q_text_loose:String}) > 0)"
        )
        rank_score_expr = (
            f"{t.lexical_score_expr} + {t.entity_boost_expr} "
            f"+ {t.recency_boost_expr} + {t.chronological_boost_expr} "
            f"+ {t.preference_boost_expr}"
        )
    elif q.search_mode == "hybrid":
        params["alpha"] = float(q.alpha)
        rank_score_expr = (
            f"{{alpha:Float32}} * ({t.lexical_score_expr}) + "
            f"(1.0 - {{alpha:Float32}}) * (1.0 - distance) + "
            f"{t.entity_boost_expr} + "
            f"{t.recency_boost_expr} + "
            f"{t.chronological_boost_expr} + "
            f"{t.preference_boost_expr}"
        )
    elif q.search_mode == "rrf":
        # Reciprocal Rank Fusion — parameter-free, robust to the
        # heterogeneous lexical (0..1 overlap) vs cosine (narrow-band) score
        # distributions that under-weight lexical under alpha-blending. The
        # SQL rank_score here is the lexical leg alone (a tiebreak); the
        # actual RRF combine runs in the Python pass below.
        rank_score_expr = (
            f"{t.lexical_score_expr} + {t.entity_boost_expr} "
            f"+ {t.recency_boost_expr} + {t.chronological_boost_expr} "
            f"+ {t.preference_boost_expr}"
        )
    elif q.search_mode == "bm25_hybrid":
        rank_score_expr = _bm25_hybrid_rank_score(q, where, params, t)
    elif q.search_mode == "graph":
        rank_score_expr = _graph_rank_score(q, params, t)
    elif q.search_mode == "related":
        # Facet-overlap retrieval for topic-clustering ("findings tagged X"):
        # rank by tag + entity intersection. Distance is computed (HNSW order)
        # but overlap fraction drives ranking.
        params["tag_filter"] = list(q.tags)
        params["ent_filter"] = [t.lower() for t in q.entity_filter]
        tag_overlap = (
            (
                "length(arrayIntersect(e.tags, {tag_filter:Array(String)})) / "
                "greatest(length({tag_filter:Array(String)}), 1)"
            )
            if q.tags
            else "0.0"
        )
        ent_overlap = (
            (
                "length(arrayIntersect(e.entities, {ent_filter:Array(String)})) / "
                "greatest(length({ent_filter:Array(String)}), 1)"
            )
            if q.entity_filter
            else "0.0"
        )
        rank_score_expr = f"({tag_overlap}) + 0.5 * ({ent_overlap}) + 0.1 * (1.0 - distance)"
        # Require at least one tag OR entity overlap so the candidate set is
        # bounded by facet relevance, not a full scan.
        facet_predicates: list[str] = []
        if q.tags:
            facet_predicates.append(
                "hasAny(e.tags, {tag_filter:Array(String)})",
            )
        if q.entity_filter:
            facet_predicates.append(
                "hasAny(e.entities, {ent_filter:Array(String)})",
            )
        if facet_predicates:
            where.append(f"({' OR '.join(facet_predicates)})")
    elif q.search_mode == "temporal":
        # Date-anchored retrieval: filter to a ±anchor_window window on
        # temporal_anchor or valid_at, cosine breaks ties within it.
        if not q.query_anchor_iso:
            # No anchor — degrade to semantic so we don't return empty.
            rank_score_expr = f"(1.0 - distance) + {t.entity_boost_expr} + {t.recency_boost_expr}"
        else:
            params["query_anchor"] = q.query_anchor_iso
            params["anchor_window"] = int(q.anchor_window_days)
            # Require the row's date in the window; undated rows can't answer
            # a dated query, so they're filtered out.
            where.append(
                "(e.temporal_anchor IS NOT NULL AND "
                "abs(dateDiff('day', e.temporal_anchor, "
                "parseDateTimeBestEffortOrNull({query_anchor:String}))) "
                "<= {anchor_window:UInt32}) "
                "OR (e.valid_at IS NOT NULL AND "
                "abs(dateDiff('day', e.valid_at, "
                "parseDateTimeBestEffortOrNull({query_anchor:String}))) "
                "<= {anchor_window:UInt32})"
            )
            # Rank within the window by cosine + entity + chronological +
            # temporal-anchor, so ordering/diff questions surface the
            # date-relevant rows, not just the cosine-nearest.
            rank_score_expr = (
                f"(1.0 - distance) + 0.5 * {t.entity_boost_expr} "
                f"+ {t.chronological_boost_expr} + {t.temporal_anchor_boost_expr}"
            )
    else:  # semantic: bare (minimal-prior) ranking
        # Pure cosine — ZERO discretionary/relevance-inflating boosts (no
        # entity/recency/chronological/temporal-anchor, no preference, and the
        # prior group + reinforcement are skipped below). Only the hard
        # demote_tags sink and the conflicted-status demotion (both appended in
        # the correctness block below) survive.
        rank_score_expr = "(1.0 - distance)"
    t.rank_score_expr = rank_score_expr


def _fact_rank_tail(q: SearchQuery, params: dict[str, Any], t: _FactRankTerms) -> None:
    """Phase 5 of `vector_search`: append the capped discretionary-prior group
    and the correctness tail (demotions, reinforcement, trust) to the mode's
    rank expression. Sets `t.rank_score_expr` and `t.prior_group_select`."""
    # Discretionary metadata priors (stored confidence, soft-tag overlap,
    # soft-category membership) + demotion. "related" has its own weighting,
    # so skip it there.
    # They are a tiebreak: on the compressed cosine band real embedders
    # produce (a great and a mediocre match ~0.1 apart), a raw confidence
    # term plus a tag/category boost can exceed the relevance gap.
    # `prior_cap` > 0 bounds the whole group so it only reorders near-ties;
    # 0.0 leaves it uncapped.
    # reinforcement is already individually capped (reinf_cap) so it stays
    # outside the group; demote is a deliberate hard sink, never a boost.
    if t.no_priors or q.search_mode == "graph":
        # Correctness-only tail: the hard demote_tags sink and the
        # conflicted-status demotion. NO prior group (confidence + soft
        # tag/category + preference), NO reinforcement boost — those are the
        # discretionary priors bare mode (and personalization="none") exist
        # to drop. confidence_weight is left unset because nothing references
        # it here.
        t.rank_score_expr = (
            f"({t.rank_score_expr}) "
            f"+ ({t.demote_expr}) + ({t.conflicted_demote_expr}) + ({t.sensitivity_demote_expr}) "
            f"+ ({t.trust_boost_expr})"
        )
    elif q.search_mode != "related":
        prior_group, capped = _prior_group_expr(
            t.tag_boost_expr,
            t.category_boost_expr,
            float(q.prior_cap or 0.0),
        )
        if capped:
            params["prior_cap"] = float(q.prior_cap)
        if t.eligible is not None:
            # The stored-confidence term lives inside the group and is a
            # user-derived prior too: gate the whole group on provenance.
            prior_group = (
                f"if(has({{sc_eligible:Array(String)}}, e.source_class), {prior_group}, 0.0)"
            )
        t.prior_group_select = prior_group
        t.rank_score_expr = (
            f"({t.rank_score_expr}) + ({prior_group}) "
            f"+ ({t.demote_expr}) + ({t.conflicted_demote_expr}) + ({t.sensitivity_demote_expr}) "
            f"+ ({t.reinforcement_boost_expr}) + ({t.trust_boost_expr})"
        )
        params["confidence_weight"] = float(q.confidence_weight)
