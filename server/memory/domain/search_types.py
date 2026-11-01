"""Core search value types.

SearchQuery / SearchHit / SearchTraceHit + the minimal CH client Protocol.
Kept in a leaf module so the LLM and BM25 helpers can import them without a
cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple, Protocol


class _CHClient(Protocol):
    async def query_all(
        self,
        sql: str,
        params: dict[str, Any] | None = ...,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SearchQuery:
    """Inputs to one memory.search call. Everything wire-validated
    Pydantic-side; here we treat it as a flat carrier of the
    parameters the SQL needs."""

    org: str
    namespace: str
    scope: str  # "private" | "shared"
    agent: str  # used when scope == "private"
    embedding_model: str
    query_vec: list[float]
    top_k: int
    memory_type: str | None = None
    user_id: str | None = None
    run_id: str | None = None
    # Read scope (server/identity/domain/read_scope.py): when set, only rows written
    # under this session are eligible. None = no session filter.
    session_id: str | None = None
    # Restrict to a SET of sessions (the caller's pick).
    # Empty = no restriction. Combines with session_id (AND).
    session_ids: list[str] = field(default_factory=list)
    entity_filter: list[str] = field(default_factory=list)
    # Category hard-filter. When set, the SQL adds a
    # WHERE clause restricting hits to memory_keys present in
    # loom.memory_categories for this category. Empty = no filter.
    category: str = ""
    # Tag filter — rows whose `tags` column intersects with this list
    # are eligible. Empty = no filter. Applied per-namespace in the
    # cross-namespace fan-out, so multi-workspace search honors the
    # filter uniformly across every namespace the search reads.
    tags: list[str] = field(default_factory=list)
    # Soft-metadata ranking. When *_soft is set, the corresponding filter
    # (tags / category) is dropped from WHERE and instead contributes an
    # additive rank boost (scaled by *_boost), so relevant rows keep surfacing
    # without the coverage loss of a hard filter. `confidence_weight` sizes the
    # stored-confidence term (default 0.1). `demote_tags`
    # pushes tag-matching rows to the bottom (large negative term) without
    # dropping them.
    tags_soft: bool = False
    tag_boost: float = 0.3
    # A `status='duplicate'` row is not served unless asked;
    # the write path's own similarity read keeps seeing every live row.
    include_duplicates: bool = False
    # A `status='superseded'` row (a later write replaced it;
    # `superseded_by` names the successor) is not served unless asked.
    include_superseded: bool = False
    # A `status='proposed'` row (a fact on trial) is served
    # only to a read that asks.
    include_proposed: bool = False
    category_soft: bool = False
    category_boost: float = 0.3
    confidence_weight: float = 0.1
    # Cap on the discretionary-prior group (confidence + soft tag/category)
    # so it acts as a tiebreak, not a re-rank. 0.0 = uncapped. See
    # the rank_score assembly in memory_search for the rationale.
    prior_cap: float = 0.0
    # Additive penalty applied to status='conflicted' rows so unresolved
    # contradictions sink below clean matches without being dropped. 0.0=off.
    conflicted_penalty: float = 0.0
    demote_tags: list[str] = field(default_factory=list)
    # Additive penalty on rows tagged `sensitivity:medium` / `sensitivity:high`
    # so a labelled contact detail does not ride into a window on a question
    # that did not ask for it. The handler sets it to 0 when
    # the query carries contact intent. 0.0 = off.
    sensitivity_penalty: float = 0.0
    # Per-request switch for the RRF adaptive lexical weight. None/True = on;
    # False = equal weights for this call, so a labelled sweep can isolate the
    # lever.
    rrf_adaptive_lexical: bool | None = None
    # Adjacent-word concatenations of the query the corpus
    # knows as one term, set by the BM25 precursor; scored beside the tokens.
    lexical_compounds: tuple[str, ...] = ()
    # Decayed-reinforcement boost: weight * log1p(reinforcement_count-1) *
    # exp(-days_since_last_reinforced / half_life), capped. 0 disables. Makes
    # recall + positive-outcome signals feed ranking, and fade over time.
    reinforcement_weight: float = 0.0
    # Rank-relative recency over the fused pool on a
    # recency-intent query; 0.0 = off.
    recency_pool_weight: float = 0.0
    # The share of the window verbatim chunks may
    # take on this read; None = the deployment setting.
    chunk_max_share: float | None = None
    reinforcement_half_life_days: float = 90.0
    # Half-life grows with use: half_life · (1 + scale · log1p(count − 1)).
    # 0 keeps the fixed half-life.
    reinforcement_half_life_use_scale: float = 0.0
    # Hybrid recall. `query_text` is the original caller string used
    # by the lexical branch (substring on content_excerpt). `search_mode`
    # picks ranker, `alpha` blends lexical and semantic when "hybrid".
    query_text: str = ""
    search_mode: str = "semantic"  # "semantic" | "lexical" | "hybrid"
    alpha: float = 0.5
    # Key-embedding axis: score each row by the closer of its
    # value and key embeddings (True), or by the value embedding alone (False,
    # which leaves the key vector column unread). None = server setting.
    key_axis: bool | None = None
    # When > 0, fetch this many extra rows by recency (or `confidence`
    # if no lexical/semantic match), regardless of similarity threshold.
    # Useful only when a reranker is wired up — gives the reranker a
    # wider candidate pool. Handler skips this when no rerank is set.
    include_top_n_unmatched: int = 0
    # Executable route mask from query_planner. Empty enables every route
    # (direct service tests/callers); handler-supplied plans
    # use this to turn chunk, lexical, graph, temporal, and unmatched
    # routes on/off deliberately.
    execution_planes: list[str] = field(default_factory=list)
    # BM25 inputs for the bm25_hybrid mode. Populated by
    # `_fetch_bm25_corpus_stats` (called from vector_search) from the
    # loom.memory_term_freq + loom.memory_doc_stats MVs. Aligned with
    # the lemmatized query tokens. Empty → fall back to token-overlap.
    bm25_tokens: list[str] = field(default_factory=list)
    bm25_idfs: list[float] = field(default_factory=list)
    bm25_avgdl: float = 0.0
    # Graph mode v1: pre-computed expansion of query entities via
    # loom.entity_edges (filled by the handler before vector_search
    # via `_fetch_graph_neighbors`). When non-empty + search_mode
    # is "graph", rows whose `entities` intersect this set get a
    # 0.3 * overlap-fraction boost on rank_score.
    graph_expanded: list[str] = field(default_factory=list)
    # graph mode boost weights.
    graph_overlap_weight: float = 0.3
    graph_triple_weight: float = 0.2
    # Graph mode v2: memory_keys whose stored triples matched
    # query-extracted triples (filled by the handler via
    # `fetch_triple_matched_memories`). Triple match is a stronger
    # signal than co-occurrence overlap — the verb-phrase aligned, not
    # just the entity pair. Rows with memory_key in this set get a
    # 0.5 boost on rank_score in graph mode.
    triple_matched_keys: list[str] = field(default_factory=list)
    # Temporal anchor — ISO date the query resolves to (after
    # `expand_temporal_query` resolves relative dates). Empty string
    # means no temporal context, no boost. Rows whose temporal_anchor
    # falls within ±`anchor_window_days` of this date get +0.4 in
    # graph mode rank_score, scaled by the row's temporal_confidence when
    # the extractor graded the date.
    query_anchor_iso: str = ""
    anchor_window_days: int = 7
    # Preference attribute — extracted from preference-shaped
    # queries ("what theme do I use" → "theme"). Memories tagged
    # `preference:THEME` at write time get a +0.3 boost when this is
    # set. Empty = no preference signal, no boost.
    preference_attribute: str = ""
    # Personalization arm. "full" = the normal ranking. "none"
    # drops every USER-DERIVED term in every mode — the preference boost, the
    # discretionary prior group (confidence / soft tag / soft category) and
    # the reinforcement boost — and keeps the relevance terms (cosine, lexical,
    # entity, recency, chronological, temporal anchor). Per request, so a
    # paired A/B runs against one server.
    personalization: str = "full"
    # source_class values (provenance.SOURCE_CLASSES) whose rows may earn the
    # user-derived terms above. None = every row (neutral). A deployment that
    # names {"asserted","observed","imported","external"} stops an LLM-derived
    # or extracted row from being boosted like something the user said.
    source_class_boost_eligible: list[str] | None = None
    # Bi-temporal as-of filter. When set, the SQL drops rows whose
    # coalesce(valid_at, temporal_anchor, created_at) > as_of (valid_time) or
    # that were written after / retired before as_of (knowledge_time). One
    # definition for every leg: server/memory/domain/temporal_sql.py.
    # `valid_at` is denormalized onto memory_embeddings
    # so this filter applies in the same WHERE pass as
    # the identity / scope filters — no hot-path join.
    as_of: datetime | None = None
    # Which time axis `as_of` replays. "valid_time" (default):
    # what was TRUE as of then — coalesce(valid_at, created_at) <= as_of.
    # "knowledge_time": what loom KNEW as of then — created_at <= as_of AND not
    # yet retired (invalid_at) or deleted (tombstoned_at) by as_of; a row
    # superseded AFTER the anchor was still believed then and comes back.
    # Ignores valid_at: a fact backdated to an earlier valid_at is still
    # excluded until the write that recorded it. Use it for "what did we know
    # at T" correctness replay.
    as_of_basis: str = "valid_time"
    # Recency-intent boost. When the query is asking about
    # CURRENT state ("what is my latest X", "where do I work now",
    # "what is my most recent Y"), bias the rank_score toward rows
    # with the newest `coalesce(valid_at, created_at)`. Detected from
    # the query text by `_detect_recency_intent`. Multiplies a small
    # recency factor (0..0.25) into rank_score so it tips ties without
    # dominating semantic similarity. Empty = no recency boost.
    recency_intent: bool = False
    # 'asc' → bias toward oldest matching memory; 'desc' → newest.
    # Set by detect_chronological_intent on comparative-order
    # questions ("which event happened first, X or Y"). Stronger than
    # recency_intent — the date is the answer, not a tiebreak.
    chronological_intent: str = ""


RowIdentity = tuple[str, str, str]


@dataclass(frozen=True)
class SearchHit:
    """One result row. The handler turns these into the wire Pydantic
    model; the service stays decoupled from wire types so it can be
    used by the dashboard's Recall Lab path too."""

    memory_key: str
    memory_type: str
    content_excerpt: str
    distance: float
    # SQL-side rank score: per-mode formula + entity-match bonus. Handler
    # uses this (not raw similarity) for cross-namespace merge ordering
    # so entity_filter boosts survive the union step.
    rank_score: float = 0.0
    # Temporal context — surfaced from loom.memory_embeddings columns
    # so the wire response carries dates directly. Empty string when
    # the row is undated. ISO format (YYYY-MM-DD or full datetime).
    temporal_anchor: str = ""
    # How precisely the source pinned temporal_anchor (0..1; 0 = unscored).
    temporal_confidence: float = 0.0
    valid_at: str = ""
    # Ingestion recency (loom.memory_embeddings.created_at), ISO string or
    # "" when unknown. Feeds the optional recency-decay rank term in the
    # search handler; transient — NOT surfaced on the wire model.
    created_at: str = ""
    # Which storage layer this hit came from. "fact" = distilled from
    # loom.memory_embeddings; "chunk" = verbatim from loom.memory_chunks.
    source: str = "fact"
    # Whether an attestation is live on the row; ranks with
    # source_class when memory_search_trust_weight > 0.
    verified: bool = False
    # Optional bridge back to the raw source resource (conversation,
    # imported artifact, document, CI run, etc.) the fact came from.
    source_resource_id: str = ""
    # The row's entities (loom.memory_embeddings.entities) — transient, read
    # by the handler to explain a graph-mode hit's path.
    entities: list[str] = field(default_factory=list)
    # The session the memory was written under (loom.memory_embeddings.
    # session_id). Provenance for the caller: a bench scores source recall
    # against gold evidence sessions, an agent groups hits by the
    # conversation they came from. "" for rows without one and for the
    # summary / derived / code planes; a chunk or record hit carries its row's.
    # Surfaced on the wire model.
    session_id: str = ""
    # Who said it: the `speaker:<name>` tag the extractor writes on a fact
    # whose cited turns all come from one named speaker; "" when the fact is
    # unattributed or an exchange. Surfaced on the wire model.
    speaker: str = ""
    # Whose fact this is: True when the row carries the
    # `about:other` tag — a preference or state of someone other than the
    # user. Surfaced on the wire model; the reader is told.
    about_other: bool = False
    # The row's tags (loom.memory_embeddings.tags), read by the dense fact leg
    # for `speaker` / `about_other` and kept here so a caller that asks
    # (`include_tags`) gets them without a memory.get per hit.
    # Empty for the lexical leg, chunks, summaries and derived rows; the
    # handler point-reads the gaps only when the request asked.
    tags: list[str] = field(default_factory=list)
    # For a digest hit (source="summary"): the memory keys the digest
    # compressed (`session_summaries.derived_from`). Lets the reader drop a
    # digest whose facts already hold window slots. Empty for every other plane.
    derived_from: list[str] = field(default_factory=list)
    # For a chunk hit (source="chunk"): the memory key of the fact the chunk
    # was written under (`memory_chunks.parent_memory_key`). The
    # reinforcement fold joins the chunk to its parent's reinforcement through it.
    # Not on the wire; "" for every other plane.
    parent_memory_key: str = ""
    # Stable, caller-facing decomposition of the final score. Values are
    # approximate for blended modes but let memory.search callers see the
    # major ranking signals.
    score_components: dict[str, float] = field(default_factory=dict)
    # Provenance class of the row (asserted | extracted | observed | derived |
    # imported | external; '' when unset).
    source_class: str = ""
    # Row state: active | superseded | duplicate | conflicted;
    # "" for chunks, summaries and derived rows. The ask pool drops superseded
    # rows and uses their text to mask the sentences that restate them.
    status: str = ""
    version: int = 0
    # The stored row's (agent, user_id, session_id) for a fact or chunk row; None for a
    # record and every other plane. Not on the wire.
    identity: RowIdentity | None = None
    # A chunk row's own `content_sha256`: with its index, which value cut it. Not on the wire.
    chunk_sha256: str = ""


def row_identity(row: Mapping[str, Any]) -> RowIdentity | None:
    """The (agent, user_id, session_id) of the memory row a leg read; None when the leg
    did not read them."""
    if any(c not in row for c in ("agent", "user_id", "session_id")):
        return None
    return str(row["agent"]), str(row["user_id"]), str(row["session_id"])


class RowKey(NamedTuple):
    """One stored row: it binds as a tuple of the five columns."""

    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str


def row_key_of(namespace: str, row: Mapping[str, Any]) -> RowKey:
    """The RowKey of a result row that read the identity columns."""
    agent, user_id, session_id = (str(row[c]) for c in ("agent", "user_id", "session_id"))
    return RowKey(namespace, agent, user_id, session_id, str(row["memory_key"]))


def _identity(hit: Any) -> RowIdentity | None:
    # `identity` on a SearchHit, the private `_row_identity` on a wire hit
    return getattr(hit, "_row_identity", None) or getattr(hit, "identity", None)


def names_row(hit: Any) -> bool:
    """Whether a hit's leg read the identity of its stored row."""
    return _identity(hit) is not None


def hit_row_key(hit: Any, namespace: str = "") -> RowKey:
    """(namespace, agent, user_id, session_id, memory_key) of the stored row a hit is: one
    key holds a row for each writer and session, so a key alone does not name one row.
    For a `SearchHit` or a wire hit alike. A hit whose leg read no identity has only its
    session."""
    identity = _identity(hit)
    agent, user_id, session_id = identity or ("", "", str(getattr(hit, "session_id", "") or ""))
    return RowKey(
        str(getattr(hit, "namespace", "") or namespace),
        agent,
        user_id,
        session_id,
        str(getattr(hit, "memory_key", "") or ""),
    )


@dataclass(frozen=True)
class SearchTraceHit(SearchHit):
    """memory.search_trace result. Adds the entity-boost contribution
    and the final rank_score so the dashboard's Recall Lab can show what blended
    into the ordering."""

    entity_boost: float = 0.0
    rank_score: float = 0.0


# The score_components entry holding a reranked row's own fused score: its rank_score is
# then the score of the rank the reranker gave it (`search/ranking.py`).
RANK_SCORE_PRE_RERANK = "rank_score_pre_rerank"


def hit_sort_key(h: Any) -> tuple[float, float, str, str, str, str, str]:
    """The one order every ranked list uses: best rank_score, nearest distance, then the
    row (memory_key, namespace, agent, user_id, session_id). Equal scores are common, so
    without the row terms the window would depend on read order, not on the hits."""
    row = hit_row_key(h)
    return (
        -float(getattr(h, "rank_score", 0.0) or 0.0),
        float(getattr(h, "distance", 0.0) or 0.0),
        row.memory_key,
        row.namespace,
        row.agent,
        row.user_id,
        row.session_id,
    )
