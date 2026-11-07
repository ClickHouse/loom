"""Shared mutable state for the memory.search pipeline.

The orchestrator settles `input` and the snapshot fields before the first
stage runs, so stages read stable values. `all_hits` / `final_hits` are
reassigned as stages progress (later stages see the update); the dict caches
(`actual_work`, the derived/temporal caches, per-namespace maps,
`entity_boost_by_key`) are mutated in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit

if TYPE_CHECKING:
    from server.embedding import Embedder
    from server.memory.app.search.budget import SearchBudget
    from server.memory.domain.query_intent import QueryIntent
    from server.memory.domain.query_planner import MemorySearchPlan


# A hit's version by (namespace, agent, user_id, session_id, memory_key).
StaleVersions = dict[tuple[str, str, str, str, str], int]


@dataclass
class SearchContext:
    """Carrier for the state the search pipeline stages share.

    Instantiated once per top-level ``handle_memory_search`` call and
    threaded through the stage functions. Recursive fallback calls
    (iterative-judge) re-enter ``handle_memory_search``
    and build their OWN context — only ``search_budget`` is shared across the
    recursion, via the input's private attr.
    """

    # --- request + collaborators (snapshot once, stable across all stages) ---
    input: MemorySearchInput
    search_budget: SearchBudget
    embedder: Embedder
    # No LLM call on this read (`_is_llm_free`): the fast posture, context.pack, or
    # LOOM_READS_LLM_FREE.
    llm_free: bool

    # --- enrichment / intent signals (settled before any leg runs) ---
    obs_date: str | None
    parsed_intent: QueryIntent | None
    resolved_dates: list[str]
    chronological_intent: str
    recency_intent: bool
    preference_attribute: str
    llm_query_entities: list[str]
    sub_queries: list[str]
    query_shape: str
    stated_value_update: bool
    parser_derived_intent: dict[str, Any] | None
    temporal_diff_spec: dict[str, Any] | None

    # --- plan + embedding + scope (settled before any leg runs) ---
    query_vec: list[float]
    search_plan: MemorySearchPlan
    namespaces: list[str]
    effective_entity_filter: list[str]
    query_anchor_iso: str
    pool_top_k: int
    endpoint: str
    prompt_template: str

    # True when `namespaces` order is authoritative (a scoped token carrying
    # an `ns_roles` claim; the addressed namespace leads, then the token's
    # order): the search fans out in this order as strict priority tiers.
    # False for unscoped/wildcard tokens (order is not meaningful; the
    # visibility-based fallback applies instead).
    namespace_order_authoritative: bool = False

    # The set-question instance phrases (plan phase 6); each runs as a supplemental leg.
    set_expansion_phrases: list[str] = field(default_factory=list)
    # --- graph-mode expansion maps (built once, then read) ---
    graph_expanded_by_ns: dict[str, list[str]] = field(default_factory=dict)
    # neighbour -> [seed, ..., neighbour] per namespace; filled by fetch_graph_neighbors
    # alongside graph_expanded_by_ns.
    graph_paths_by_ns: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    triple_matched_by_ns: dict[str, list[str]] = field(default_factory=dict)
    query_triples: list[tuple[str, str, str]] = field(default_factory=list)

    # --- mutable accumulators / caches ---
    # actual_work: instrumentation dict, mutated in place by every stage.
    actual_work: dict[str, Any] = field(default_factory=dict)
    # The orchestrator's StageClock, so a stage that runs inside another function (digest
    # descent inside the assembler) can name its own time instead of hiding in the enclosing one.
    clock: Any = None
    # entity-store ANN boost map, joined just before the global fusion pass.
    entity_boost_by_key: dict[tuple[str, str], float] = field(default_factory=dict)
    # Lazy derived-answer / temporal-diff caches (compute-at-most-once).
    derived_aggregate_cache: dict[str, Any] = field(default_factory=dict)
    temporal_diff_cache: dict[str, Any] = field(default_factory=dict)
    # The evolving candidate pool, then the assembled / sliced result list.
    # These two are the ONLY fields reassigned while stages are executing.
    all_hits: list[_MemorySearchHit] = field(default_factory=list)
    final_hits: list[_MemorySearchHit] = field(default_factory=list)
    # The ranked pool past the served block, and the distinct candidate count, filled by the
    # assemble stage.
    page_tail: list[_MemorySearchHit] = field(default_factory=list)
    candidates_total: int = 0
    # Under the Postgres authority: the hits validation dropped (`pg_validation`).
    stale_dropped: int = 0
    # The versions it dropped, so a tier does not count them again.
    stale_versions: StaleVersions = field(default_factory=dict)

    # The recall-credit verdict for this read (`credited` | `not_credited (<reason>)`), None
    # when auto_touch was off or nothing was served; rides the response as `reinforcement`.
    recall_credit: str | None = None
