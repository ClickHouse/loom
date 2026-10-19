"""memory.search: legs, fusion, ranking and gates."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.settings._env import CaseFolded
from server.settings._live import LIVE


class MemorySearchSettings(BaseModel):
    # Default `namespace_scope` when a request leaves it unset. "this" reads
    # the addressed namespace only; "all_mine" also reads the caller's own
    # namespaces (the MCP loom_search asks for it by name), see
    # docs/concepts/identities-and-namespaces.md#reading-several-namespaces.
    memory_search_default_namespace_scope: Annotated[Literal["this", "all_mine"], LIVE] = "this"
    # Namespace kinds an all_mine fan-out spans (memory.search,
    # knowledge.search, conversation.search, MCP loom_search): comma list over
    # loom.namespaces.kind; a namespace with no kind row stays; "" = every
    # kind. Code and QA indexes are reached by name or their own tools
    # (measured sweep of 24 namespaces: 6.2 s).
    memory_search_fanout_kinds: Annotated[str, LIVE] = "memory"
    # Upper bound on how many of the caller's namespaces an all_mine fan-out
    # reads besides the addressed one; a search names the rest in
    # `namespaces_skipped`. Every leg issues at least one statement per
    # namespace. A guard, not a tuning knob: set well above what one person
    # holds, so it bounds a runaway sweep rather than trimming normal ones.
    memory_search_fanout_max_namespaces: Annotated[int, LIVE] = Field(default=32, ge=1)
    # Key-embedding axis on the fact leg: "on" scores least(value_dist,
    # key_dist) and reads both vector columns (measured 399 vs 203 MB/query on
    # a 30k-row namespace); "off" reads the value vector only; "auto" turns it
    # on per namespace when its key-reuse share is at or above
    # memory_search_key_axis_auto_reuse_share (cached for
    # memory_search_key_axis_auto_ttl_seconds; a failed probe reads as off).
    # A per-request `key_axis` wins.
    memory_search_key_axis: Annotated[Literal["auto", "on", "off"], CaseFolded, LIVE] = "auto"
    # "auto" key-axis threshold: share of a namespace's keys written more than
    # once. 0.05 sits between product namespaces and chat namespaces.
    memory_search_key_axis_auto_reuse_share: Annotated[float, LIVE] = 0.05
    # Seconds a namespace's "auto" key-axis probe result is cached before it is re-read.
    memory_search_key_axis_auto_ttl_seconds: Annotated[int, LIVE] = 600
    # No-probe fallback for the query-rewrite gate the calibration reports
    # (calibration.get's scalar row): ≈ question→fact P10 for
    # text-embedding-3-small.
    memory_search_query_rewrite_threshold: float = 0.38

    # Bounds on the recursive fallback cascade (iterative-judge retry),
    # shared through one SearchBudget: recursive rounds and wall-clock ms;
    # whichever trips first stops further fallbacks. The first-pass retrieve
    # always runs.
    memory_search_fallback_max_rounds: Annotated[int, LIVE] = 1
    # Wall-clock ms the fallback cascade may spend across all rounds before it stops.
    memory_search_fallback_max_wall_ms: Annotated[int, LIVE] = 4000
    # Finer caps inside a round: LLM calls and vector queries a fallback may
    # spend before it is denied.
    memory_search_fallback_max_llm_calls: Annotated[int, LIVE] = 2
    # Vector queries one fallback round may issue before further ones are denied.
    memory_search_fallback_max_vector_queries: Annotated[int, LIVE] = 12

    # First-stage pool = top_k × multiplier, per retrieval profile, capped at
    # memory_search_max_pool_top_k (all_mine searches multiply by namespace
    # and sub-query count).
    memory_search_pool_multiplier_standard: Annotated[int, LIVE] = 3
    # Rerank gets the widest pool: a reranker only reorders what the first
    # stage surfaced (measured: items it should promote sat at rank 11-30).
    # Affects rerank-enabled searches only.
    memory_search_pool_multiplier_rerank: Annotated[int, LIVE] = 6
    # A deep-budget search's pool multiplier (the budget class `deep`).
    memory_search_pool_multiplier_deep: Annotated[int, LIVE] = 5
    # Upper bound on the first-stage pool size; a caller's top_k above it raises the bound to
    # top_k.
    memory_search_max_pool_top_k: Annotated[int, LIVE] = 150
    # Pool widening factor for a quality-filtered search (loom_search,
    # context.pack), applied before junk is dropped.
    memory_search_quality_pool_multiplier: Annotated[int, LIVE] = 3

    # Boost for any preference-typed/tagged row on any query; 0.0 makes it
    # intent-gated (only a query naming the attribute earns +0.3).
    memory_search_preference_fallback: Annotated[float, LIVE] = 0.15
    # Comma list of source_class values whose rows may earn the user-derived
    # rank terms (preference, prior group, reinforcement); "" = every row.
    # Leaving out "extracted" keeps an inference from outranking a statement
    # by its priors.
    memory_search_source_class_boost_eligible: Annotated[str, LIVE] = ""
    # Top-hit similarity below which a search abstains (weak abstention): the
    # scalar gate for fake embedders or LOOM_ADAPTIVE_THRESHOLDS=false; a
    # calibrated namespace or embedder derives its own.
    memory_search_abstain_threshold: float = 0.30

    # Identifier-grounding abstain: when the query carries a salient
    # identifier (PR/issue number, test id, @handle) and no returned hit
    # contains any of them, abstain with abstain_code "entity_absent"
    # regardless of the top cosine — catches the on-topic but wrong neighbour
    # the cosine gate cannot.
    memory_search_identifier_grounding: Annotated[bool, LIVE] = True
    # The grounding leg applies only to queries of at most this many
    # whitespace tokens; longer inputs (a PR body, a path list) carry
    # incidental numbers. 0 = no cap.
    memory_search_identifier_grounding_max_tokens: Annotated[int, LIVE] = 32

    # Recency-intent searches ("latest", "current …") re-rank the fused pool
    # by date: the newest dated hit gains this much, the oldest and undated
    # hits nothing. 0.0 = off. A namespace can set its own in
    # retrieval_defaults.
    memory_search_recency_pool_weight: Annotated[float, LIVE] = 0.0

    # No-probe fallback for the question→fact P25 gate (≈ P25 for
    # text-embedding-3-small): the loom_search precision floor and
    # calibration.get's scalar row read it.
    memory_search_hyde_fallback_threshold: float = Field(default=0.45, gt=0.0, le=1.0)
    # search_mode used when the caller sets neither search_mode nor a recipe.
    # The wire default stays "rrf"; planner shape rules still apply on top.
    memory_search_default_mode: Annotated[
        Literal[
            "semantic", "lexical", "hybrid", "bm25_hybrid", "related", "graph", "rrf", "temporal"
        ],
        CaseFolded,
        LIVE,
    ] = "semantic"
    # Weight of the decayed-reinforcement rank term (recalls and positive
    # memory.outcome signals, fading with time since last reinforcement).
    # 0 disables; the term is capped below relevance.
    memory_search_reinforcement_weight: Annotated[float, LIVE] = 0.15
    # Additive penalty on rows tagged `sensitivity:medium|high` unless the
    # query has contact intent. 0.0 disables.
    memory_search_sensitivity_demote: Annotated[float, LIVE] = 0.1
    # Days after which a use's reinforcement weighs half (the base half-life
    # the use-scaled one below grows from).
    memory_search_reinforcement_half_life_days: Annotated[float, LIVE] = 90.0
    # Per-query recall telemetry into loom.search_trace_log (query, returned
    # keys+scores, abstain, latency), fire-and-forget. Off by default: query
    # text is logged, so it is an operator opt-in.
    search_trace_log_enabled: Annotated[bool, LIVE] = False
    # Past queries as nodes: one loom.query_nodes row per memory.search
    # (normalised text, embedding, returned keys), gated on the namespace's
    # capture consent; the erasure saga deletes the table.
    memory_query_nodes_enabled: Annotated[bool, LIVE] = False
    # Set-expansion instance legs (collective nouns -> "has a sister"): each
    # phrase runs as a narrow sub-query leg of top_k rows; a row joins only
    # above min_similarity, at most max_added per search, never above the main
    # leg's best row.
    memory_search_set_expansion_leg_top_k: Annotated[int, LIVE] = 2
    # Similarity a set-expansion leg's row must exceed to join the pool.
    memory_search_set_expansion_min_similarity: Annotated[float, LIVE] = 0.5
    # Most rows the set-expansion legs add to one search.
    memory_search_set_expansion_max_added: Annotated[int, LIVE] = 5
    # Read-side exposure ledger in loom.memory_exposures: one row per read
    # (token subject, returned keys, query digest never the text, scope) plus
    # an hourly per-actor roll-up. Carries no user content.
    memory_exposure_ledger_enabled: Annotated[bool, LIVE] = True
    # Cosine floor: hits below it are dropped as noise (ANN always returns
    # top_k). The backstop when rerank is bypassed or fails.
    memory_search_min_similarity: float = 0.22
    # Sub-floor handling when the caller sets top_k: "readmit" (fill k, tag
    # below_floor), "ids_only" (fill k with empty bodies), "drop". A request's
    # floor_mode overrides.
    memory_search_floor_mode: Annotated[
        Literal["readmit", "ids_only", "drop"], CaseFolded, LIVE
    ] = "readmit"
    # Per-attempt bound (seconds) on the query embedding; after the retries
    # the search answers 503 EMBED_TIMEOUT. 0 = unbounded.
    memory_search_embed_bound_s: Annotated[float, LIVE] = 2.5
    # Retries after a timed-out first attempt, each with the bound above.
    memory_search_embed_bound_retries: Annotated[int, LIVE] = 1
    # A query of at most this many words embeds each all-lowercase word of 4+
    # letters in the identifier spelling the caller's rows in the addressed
    # namespace use most ("datalakecatalog" -> "DataLakeCatalog"; never
    # "what" -> "What"). One read of the namespace beside the embed (100-300 ms
    # for 40k rows), reused for a minute; fail-open. 0 = off.
    memory_search_query_spelling_max_words: Annotated[int, LIVE] = Field(default=6, ge=0)
    # Body length (chars) for bodies="excerpt" (the MCP search default).
    memory_search_excerpt_chars: Annotated[int, LIVE] = Field(default=200, ge=1)
    # Above this top_k a search that pinned no `bodies` serves excerpts, and
    # per-hit explanation / score_components ride only under `explain: true`.
    # Keys, order and scores are unchanged. 0 = never.
    memory_search_excerpt_bodies_k: Annotated[int, LIVE] = 15

    # Visibility-ordered private-first fallback (non-scoped tokens): the best
    # private-namespace similarity that skips the public pass. "Private" is
    # namespace visibility, not the caller's role; scoped tokens use their
    # priority order instead.
    memory_search_private_first_stop_similarity: Annotated[float, LIVE] = 0.30

    # Tier waterfall stop: a per-tier best similarity at or above this stops
    # the search before lower-priority tiers. A control cutoff, not a return
    # filter (memory_search_min_similarity still gates rows); keep it above
    # the noise band (~0.10-0.25) or a middling hit hides a stronger one.
    memory_search_tier_stop_similarity: Annotated[float, LIVE] = 0.40

    # A key matches when it covers at least this share of the query's
    # idf-weighted stems (idf over the namespace's own keys).
    memory_search_key_match_min_strength: Annotated[float, LIVE] = 0.5
    # Boost (cosine units) of a full-strength key match: a tie-break inside
    # the pool's band, not a lever over it (a 0.5 scale lifted wrong rows
    # over gold).
    memory_search_key_match_boost_scale: Annotated[float, LIVE] = 0.03
    # Tie-break boost (cosine units) for a hand-asserted fact row (source_class
    # 'asserted', not a hint:/procedure/summary:/obs: row) against derived
    # rows; never a lever over a stronger match. 0 disables.
    memory_search_asserted_boost_scale: Annotated[float, LIVE] = 0.03
    # When the key-match arm runs: "always" (every search; measured 585-781 ms
    # p50 on live namespaces), "gated" (only when an added row could reach the
    # served window: rrf/rerank pool, pool short of top_k, an under-covered
    # identifier, or a pooled key already matching), "off".
    memory_search_key_match_mode: Annotated[Literal["always", "gated", "off"], LIVE] = "gated"
    # Rows per memory_key in the final block (0 = no cap), freeing slots taken
    # by one key re-extracted per session (38 of 60 questions on one eval).
    # Skipped for aggregational / temporal_diff shapes.
    memory_search_per_key_cap: Annotated[int, LIVE] = 3
    # Digest-anchored expansion: a session digest in the pool's top-m
    # summaries adds that session's top-k facts as candidates (+300 ms ask
    # p50); MemorySearchInput.digest_descent overrides. chunk_date_source:
    # "session" dates a chunk by its conversation's digest, "parent" by the
    # session's first fact anchor.
    memory_search_chunk_date_source: Annotated[Literal["session", "parent"], LIVE] = "session"
    # Top session digests in the pool that digest descent expands.
    memory_search_digest_descent_top_m: Annotated[int, LIVE] = Field(default=3, ge=1)
    # Facts digest descent adds as candidates from each expanded session.
    memory_search_digest_descent_k: Annotated[int, LIVE] = Field(default=5, ge=1)
    # graph_seed: query | recent | both — under recent/both the entities of
    # the identity's newest memories (recent_memories, at most recent_max)
    # join the graph seeds. diversify: "none" | "session" — under "session" at
    # most diversify_session_cap hits per source session (0 = max(2,
    # top_k // 3)); per-request `diversify` overrides.
    memory_search_graph_seed: Annotated[Literal["query", "recent", "both"], LIVE] = "query"
    # Newest memories of the identity whose entities seed the graph under recent/both.
    memory_search_graph_seed_recent_memories: Annotated[int, LIVE] = Field(default=5, ge=1)
    # Most recent-memory entities added to the graph seeds under recent/both.
    memory_search_graph_seed_recent_max: Annotated[int, LIVE] = Field(default=8, ge=1)
    # bm25_hybrid merges the once-a-minute BM25 roll-ups with a live aggregate
    # over rows newer than their mark, so df and corpus size are exact. Off =
    # roll-ups alone (up to ~70 s behind a write).
    memory_search_bm25_live_tail: Annotated[bool, LIVE] = True
    # Seconds a parked page (a `paging=true` search's tail) stays fetchable
    # before the TTL merge reclaims it.
    memory_search_page_ttl_s: Annotated[int, LIVE] = Field(default=1800, ge=1)

    # Entity-boost candidate shape (server/memory/app/search/ann_entity_boosts.py).
    # loom.entity_vectors holds one row per (entity, memory_key) edge, so the
    # lookup ranks distinct entities first, then expands to edges (a flat
    # LIMIT surfaced as few as 1 distinct entity). top_entities = distinct
    # entities kept; max_pairs = hard bound on the edge expansion (a hub can
    # carry hundreds), which bounds work rather than results.
    memory_search_entity_boost_top_entities: Annotated[int, LIVE] = 20
    # Most (entity, memory_key) edges the entity-boost lookup expands to.
    memory_search_entity_boost_max_pairs: Annotated[int, LIVE] = 400
    # Weight on the provenance term (source_class + verified); 0 = off, a
    # strict no-op in every mode.
    memory_search_trust_weight: Annotated[float, LIVE] = 0.0
    # Prepend the extractor's `question_topic` to the embedded text of
    # assistant-direct-answer facts, so a topic-only follow-up matches them.
    # The stored value is untouched; takes effect on (re-)ingest.
    memory_embed_answer_topic: Annotated[bool, LIVE] = True
    # Pre-built entity summaries fetched per namespace on that leg.
    memory_search_entity_summaries_top_n: Annotated[int, LIVE] = 5

    # Per-token top_k ceiling by the token's role on the namespace: viewer and
    # writer each have one, admin and above are uncapped. 0 = no ceiling. The
    # applied ceiling rides the response as `top_k_ceiling` and the ledger as
    # `out_top_k_ceiling`.
    memory_search_top_k_ceiling_viewer: Annotated[int, LIVE] = 0
    # top_k ceiling for a writer token on the namespace; 0 = no ceiling.
    memory_search_top_k_ceiling_writer: Annotated[int, LIVE] = 0

    # RRF adaptive lexical weight: code/path-shaped queries (CamelCase,
    # extensions, separators, digit runs) down-weight the lexical leg so
    # fusion leans semantic; prose keeps full weight.
    memory_rrf_code_shape_lo: Annotated[float, LIVE] = (
        0.20  # shape at/below which w_lex == 1.0 (prose)
    )
    memory_rrf_code_shape_hi: Annotated[float, LIVE] = (
        0.30  # shape at/above which w_lex == floor (code)
    )
    memory_rrf_lexical_weight_floor: Annotated[float, LIVE] = (
        0.1  # never fully zero the lexical leg
    )

    # Smallest window share verbatim chunks keep when the pool has them.
    # 0.0 = off.
    memory_search_chunk_min_share: Annotated[float, LIVE] = 0.0
    # Which cosine the abstain gate reads: "first" = the fused rank-1 row,
    # "window" = the best cosine among returned rows.
    memory_search_abstain_top_sim_scope: Annotated[str, LIVE] = "first"
    # Codes-first main fact leg (`vector_search_use_quantized_codes = 1`),
    # safe only while loom.memory_embeddings has no patch part (ClickHouse
    # skips the lazy rescore under patch parts). "auto" follows a 60 s
    # system.parts probe (gauge loom.memory_embeddings.patch_parts); "on" /
    # "off" pin it. The setting ships only where
    # the replica probe finds it.
    memory_search_fact_codes_first: Annotated[Literal["auto", "on", "off"], CaseFolded, LIVE] = (
        "auto"
    )
    # Over-fetch of the codes pass on the main leg; x1 is too low (it changes the
    # top-10 on a quarter of queries).
    memory_search_fact_codes_fetch_multiplier: Annotated[float, LIVE] = 3.0
    # `max_threads` pin on the main fact statement; 0 = no pin (threads follow
    # the marks); 4 bounds each statement to four threads. Unpinned costs more
    # CPU and peak memory per statement.
    memory_search_fact_leg_max_threads: Annotated[int, LIVE] = 0
