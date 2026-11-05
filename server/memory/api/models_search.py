"""memory.* retrieval ops — search, search_trace, aggregate."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from server.memory.api.models_common import MemoryType, _Identity
from server.memory.domain.code_change import CodeState
from server.memory.domain.rerank_names import RerankName
from server.operations.base import MCP_HIDDEN

# ============================================================
# memory.search
# ============================================================


SEARCH_TOP_K_MAX = 500  # not a setting: the wire bound of memory.search's top_k


class MemorySearchInput(BaseModel):
    """Semantic search over the agent's own memory.

    Only `semantic`, `procedural`, and `preference` memories are embedded
    at set-time; `episodic` memory is session-scoped and not searchable.

    `namespace_scope` controls which namespaces the query spans:
      • "this"      — `input.namespace` only.
      • "all_mine"  — also the caller's own namespaces, up to the fan-out cap
                      (the rest in `namespaces_skipped`); see
                      docs/concepts/identities-and-namespaces.md#reading-several-namespaces.
      • unset       — the deployment default (setting
                      `memory_search_default_namespace_scope`, default
                      "this").
    The response names the namespaces in scope (`namespaces_searched`) and
    marks every hit from a namespace other than the addressed one with
    `score_components.foreign = 1.0`.
    """

    org: str
    namespace: str
    # Defaults to "*" = read ALL agents in the accessible namespaces:
    # membership is the read boundary, agent is authorship metadata. Pass an
    # explicit agent only to narrow results to that one author's rows.
    agent: str = "*"
    # render="reader": also return `reader_block` + `reader_instructions`,
    # the hits rendered and instructed the way memory.ask reads them.
    # The hits themselves are unchanged.
    render: Literal["", "reader"] = ""
    # The natural-language question or keywords to search for; it is embedded for the
    # semantic legs and tokenized for the lexical ones.
    query: str
    # None = every searchable type except the hidden-by-default ones
    # (reflection). "reflection" = only process lessons (what worked / failed
    # in past runs), which never surface in an unfiltered search.
    memory_type: (
        Literal["semantic", "procedural", "preference", "reflection", "decision"] | None
    ) = None
    # OR-filter on tags (set semantics). Empty = no tag filter. Prefer tags over
    # memory_type for any discrimination finer than the coarse type enum.
    tags: list[str] = Field(default_factory=list)
    # Which rows to read: "private" = per-agent rows (narrowed to `agent` when it names
    # one), "shared" = rows promoted to the namespace's shared scope.
    scope: Literal["private", "shared"] = "private"
    # "this" reads input.namespace only; "all_mine" also the caller's own
    # namespaces, see docs/concepts/identities-and-namespaces.md#reading-several-namespaces.
    # None → `memory_search_default_namespace_scope` (default "this").
    namespace_scope: Literal["this", "all_mine"] | None = None
    # Recall fusion mode. Default "rrf" = Reciprocal Rank Fusion of lexical +
    # semantic — parameter-free and score-scale-independent, so heterogeneous
    # score distributions fuse without per-corpus alpha tuning. Others:
    #   "semantic"     — cosine only
    #   "lexical"      — token-overlap only (code symbol search)
    #   "hybrid"       — alpha*lexical + (1-alpha)*cosine blend
    #   "bm25_hybrid"  — full BM25 (IDF-weighted) fused with cosine
    #   "graph"        — entity-edge expansion + triple-match boosts
    #   "related"      — facet-overlap (tags + entity_filter)
    search_mode: Literal[
        "semantic",
        "lexical",
        "hybrid",
        "bm25_hybrid",
        "related",
        "graph",
        "rrf",
        "temporal",
    ] = "rrf"
    # Named presets over the lower-level knobs. Empty = use caller fields as-is;
    # non-empty applies a server-side profile before retrieval.
    recipe: Literal[
        "",
        "fast",
        "balanced",
        "quality",
        "verbatim",
        "code",
        "temporal",
        "graph",
    ] = ""
    # Lexical weight for `search_mode` "hybrid" / "bm25_hybrid": the score is
    # alpha * lexical + (1 - alpha) * cosine. Ignored by the other modes.
    alpha: float = Field(default=0.5, ge=0.0, le=1.0)
    # When > 0 and a reranker is configured, fetch this many extra candidates by
    # recency to widen the reranker's pool. No-op with no reranker.
    include_top_n_unmatched: int = Field(default=0, ge=0, le=200)
    # Carry each hit's `tags` on the response. Off by default (the key is
    # absent, not empty); on, a fact hit carries the row's tags (the dense leg already
    # reads the column; the few hits from other legs are point-read once) and
    # a chunk / digest / derived hit carries []. A caller whose classification
    # lives in tags ("kind:lesson", "source:…") reads it from the hit instead
    # of one memory.get per hit.
    include_tags: bool = False
    # Carry each hit's reliance counters on the response —
    # `reinforcement_count` (the folded rank signal), `used_count` (an agent's
    # label: memory.outcome used / run.complete memories_used),
    # `used_cited_count` (memory.ask's own credit for citing the key),
    # `harm_count`, `evidence_count`, `acted_count` and `pinned`. Off by
    # default (the keys are absent, not zero); on, three keyed point-reads
    # over the served fact keys, fail-open.
    include_usage: bool = False
    # A `status='duplicate'` row (set-time dedup at 0.97 cosine
    # named its canonical in `duplicate_of`) is not served; its canonical
    # takes the slot, disclosed on the hit as `resolved_from`. True returns
    # the duplicates in their own slots. `retrieval_work` counts
    # `duplicates_hidden` / `duplicates_resolved` when any were.
    include_duplicates: bool = False
    # A `status='superseded'` row (a later write replaced it)
    # is not served unless asked. With `include_superseded: true` the retired
    # rows return in their own slots, each with `superseded_by` (the
    # successor's key) and `superseded_at`, so "what did we use before" and
    # "why did it change" can be answered from the chain. `retrieval_work`
    # counts `superseded_served` when any were.
    include_superseded: bool = False
    # `status='proposed'` rows (facts written for trial,
    # accepted or rejected on graded run outcomes) are served only here, when
    # asked. A run that opts in and relies on one names it in run.complete's
    # memories_used, so a later run.grade can count the outcome against it.
    include_proposed: bool = False
    # Carry per-hit `explanation` and `score_components`.
    # None = the server default: on at top_k <= memory_search_excerpt_bodies_k
    # (15), off above it. True / False pin it for this call. Above the same k an
    # unpinned `bodies` serves the excerpt tier (memory_search_excerpt_chars)
    # instead of the full clip.
    explain: bool | None = None
    # The legs the caller can show. None = every leg
    # (memory.ask, the MCP tools, the SDKs). The dashboard's
    # Memories page passes ["memory"]: it renders memory cards and never a
    # session digest. "memory" is always searched; "sessions" gates
    # the session-summary legs. Hidden from the MCP listing.
    legs: list[Literal["memory", "sessions"]] | None = None
    # Record append-only recall usage for every returned row (powers recall_count /
    # last_recalled_at via loom.memory_usage_*, no read-path mutation of loom.memory).
    # Default ON so MCP callers, which wouldn't otherwise set it, still get telemetry.
    auto_touch: bool = True
    user_id: str | None = None  # filter by user_id (for preference recall)
    run_id: str | None = None  # only rows written under this run; unset or "-" reads every run
    # Filter by session_id. None = no session filter. Also the identity that
    # read_scope="own_session" binds to.
    session_id: str | None = None
    # Restrict to a set of sessions: a caller may pin its
    # own. Empty = no filter.
    session_ids: list[str] = Field(default_factory=list, max_length=200)
    # Keys the caller already holds and does not want served again: dropped
    # from the ranked pool BEFORE the window is assembled, so the window fills
    # from the next-best candidates instead of returning a shorter list. An
    # agent loop re-reads many of the same keys within minutes; keeping what
    # it saw lets it stop paying for them. Empty = unchanged.
    exclude_keys: list[str] = Field(default_factory=list, max_length=200)
    # Drop stored rows that are not facts (trajectory steps,
    # session narration, tool tips, code-location trivia, probes, one-moment
    # status — server/memory/domain/fact_quality.py) and restatements of a
    # higher-ranked fact from the pool before the window is cut, so the window
    # is filled with real facts. The agent-facing reads (loom_search,
    # context.pack) set it; memory.search leaves it off for benches and SDK
    # callers.
    quality_filter: bool = Field(default=False, json_schema_extra=MCP_HIDDEN)
    # How far this read may reach inside each namespace:
    #   "namespace"   — every row the token can access (membership is the read
    #                   boundary; the default).
    #   "own_user"    — only rows written under `user_id`, which must be set.
    #   "own_session" — only rows written under `session_id`, which must be set.
    # The token's `read_scope` claim is a ceiling: a request may narrow
    # further, never widen. One agent serving many end users through one token
    # should carry own_user, so a prompt from one user cannot pull another
    # user's memories into its context.
    read_scope: Literal["namespace", "own_user", "own_session"] = "namespace"
    entity_filter: list[str] = Field(
        default_factory=list,
        description="If non-empty, OR-match against extracted entities — "
        "rows whose `entities` array intersects this list are "
        "boosted alongside cosine matches.",
    )
    category: str = Field(
        default="",
        description="If set, restrict the search to memories assigned to "
        "this category (via namespace.register_category + "
        "memory.assign_category). Hard filter by default — only members of "
        "the category are returned; set category_soft=True to make it a "
        "ranking boost instead. Empty = no category scoping.",
    )
    # --- Soft metadata ranking (sharpen recall without the coverage loss of a
    # hard filter). `tags`/`category` normally hard-filter; set *_soft=True to
    # turn them into an additive rank boost: relevant rows still surface, but
    # tag/category-overlapping rows rank higher. Mirrors how `entity_filter`
    # already behaves. Defaults preserve the hard-filter contract. ---
    tags_soft: bool = False
    tag_boost: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Max rank contribution from tag overlap when tags_soft "
        "(scaled by fraction of query tags present).",
    )
    # True turns `category` from a hard filter into a rank boost of
    # `category_boost` for its members; False (default) returns members only.
    category_soft: bool = False
    category_boost: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Rank contribution for category members when category_soft.",
    )
    # Per-request weight on stored `confidence` in the blended rank score.
    # Default 0.1; 0 disables it.
    confidence_weight: float = Field(default=0.1, ge=0.0, le=1.0)
    # Cap on the discretionary-prior group (confidence + soft tag/category)
    # so it tiebreaks rather than re-ranks. None -> deployment default
    # (LOOM_MEMORY_PRIOR_CAP); 0.0 = uncapped.
    prior_cap: float | None = Field(default=None, ge=0.0, le=1.0)
    # Demotion applied to rows tagged `sensitivity:medium|high`. None ->
    # deployment default (LOOM_MEMORY_SEARCH_SENSITIVITY_DEMOTE),
    # zeroed by the handler when the query carries contact intent; an
    # explicit value wins over both. 0.0 = rank the labelled row like any other.
    sensitivity_penalty: float | None = Field(default=None, ge=0.0, le=1.0)
    # RRF only: down-weight the lexical leg for code/path-shaped queries.
    # None/True = on; False = equal weights for this call, so an eval sweep
    # can isolate it.
    rrf_adaptive_lexical: bool | None = None
    # Hard-demote: rows whose `tags` intersect this list are pushed to the
    # bottom of the ranking (large negative term) but NOT dropped — for
    # known-noise markers (e.g. "recurring_false_positive") that still surface.
    demote_tags: list[str] = Field(default_factory=list)
    # Decayed-reinforcement boost overrides (None → server default). weight sizes
    # the boost from reinforcement_count (recall + positive memory.outcome),
    # half_life_days sets how fast it decays since last reinforcement.
    reinforcement_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    # Per-request override of the recency pool re-rank weight
    # applied on a recency-intent query; None = the namespace's
    # retrieval_defaults, else the deployment default (0.0 = off).
    recency_pool_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    # Days for the reinforcement boost to halve since a row was last
    # reinforced; None = the deployment default.
    reinforcement_half_life_days: float | None = Field(default=None, ge=1.0)
    # Pool-consensus mention boost: hits whose entities the
    # rest of the candidate pool also mentions move up by at most prior_cap.
    # None or 0 = off.
    mention_boost: float | None = Field(default=None, ge=0.0, le=1.0)
    # Append the row's resolved date to a fact excerpt that
    # still carries a bare relative phrase ("… yesterday (on 2023-11-16)").
    # None = on; False turns it off for this call.
    ground_relative_dates: bool | None = None
    # Use-scaled half-life: half_life · (1 + scale ·
    # log1p(reinforcement_count − 1)). None or 0 = fixed half-life.
    reinforcement_half_life_use_scale: float | None = Field(default=None, ge=0.0, le=10.0)
    # Key-match arm override: None → deployment default
    # (LOOM_MEMORY_SEARCH_KEY_MATCH_MODE); True/False forces it for this request.
    key_match: bool | None = None
    # `always` | `gated` | `off` for this request; None → the
    # deployment default (LOOM_MEMORY_SEARCH_KEY_MATCH_MODE). `key_match`
    # (True/False) still wins when set. Lets an A/B pin the gate per call.
    key_match_mode: Literal["always", "gated", "off"] | None = None
    # When a session digest ranks in the top-m of the pool,
    # pull that session's own top-k facts in as candidates. None = on; False
    # turns it off for this call.
    digest_descent: bool | None = None
    # The calling agent's role in a multi-agent run — planner, executor,
    # critic, or the team's own word. Becomes a SOFT tag
    # `role:<role>`: same-role rows (a reflection from a run that declared
    # the role) earn the tag-overlap boost; nothing is filtered out.
    role: str = Field(default="", max_length=64)
    # At most this many hits to return (capped at 500, and lower by a per-token
    # ceiling reported in `top_k_ceiling`).
    top_k: int = Field(default=5, ge=1, le=SEARCH_TOP_K_MAX)
    # Key-embedding axis. True: a row's distance is the closer
    # of its value and key embeddings (the stored default). False: value
    # embedding only, so the fact leg reads one vector column instead of two
    # (about half the bytes per query on a 30k-row namespace). None = the
    # server setting LOOM_MEMORY_SEARCH_KEY_AXIS.
    key_axis: bool | None = None
    # What each hit carries back. A hit's key is a handle; the
    # body is context the agent can be made to repeat, so a
    # caller that only needs to know WHAT matched should not receive it.
    #   "full"    — content_excerpt as stored (write-time clip; the default).
    #   "excerpt" — the first LOOM_MEMORY_SEARCH_EXCERPT_CHARS (200) chars, a
    #               decision or constraint row its stored clip whole;
    #               memory.get fetches the rest on demand, and is itself a
    #               recorded exposure.
    #   "none"    — keys, types and scores only.
    #   "unit"    — the STORED VALUE, whole, filled by rank until
    #               body_budget_tokens is spent; the rest keep the clip and
    #               carry `next` (a memory.get call) with body_truncated=true.
    #               For a consumer that must reuse an exact earlier value
    #               (a derivation, a config block, a decision with its
    #               constants): the write-time clip is 1,200 chars, enough to
    #               show a lemma's statement but not its proof. Same contract
    #               as code.enclosing include_body.
    #   "source"  — as "unit", then every fact hit that links to a source
    #               evidence row (memory_belief_sources → memory_evidence)
    #               is served that passage instead, once per passage, by
    #               rank, within body_budget_tokens (the
    #               facts index the traces, the traces are what is read).
    bodies: Literal["full", "excerpt", "none", "unit", "source"] = "full"
    # Cap 32,000: a run trace is ~4,000 tokens whole, and a constraint that
    # cites three earlier steps needs three whole traces plus the base; a
    # 12,000 cap clips the third to 1,200 chars without its result. The
    # caller sets the budget; bodies="unit" is opt-in.
    body_budget_tokens: int = Field(default=4000, ge=200, le=32000)
    # bodies="source": 0 serves the whole linked
    # passage; N serves an N-character window centred on the span the fact
    # was extracted from (memory_belief_sources.char_start/char_end): an
    # evidence capsule at a fraction of the trace-sized window.
    source_span_chars: int = Field(default=0, ge=0, le=20000)
    # Paging. `paging=true` asks for a `next_page` handle
    # when the ranked pool held more than top_k; None or false = no handle.
    # `page=<handle>` serves the next
    # slice of a list an earlier call parked — no retrieval runs, the read
    # scope is the one the first call resolved, and `candidates_total` is the
    # first call's count.
    paging: bool | None = None
    # A `next_page` handle from an earlier call; serves that list's next slice with no
    # retrieval. Empty = run a fresh search. An expired handle abstains `page_expired`.
    page: str = Field(default="", max_length=120)
    # Run-scoped recency tier. When > 0, the N most recently
    # written memories of this identity — restricted to `run_id` when the
    # request carries one — are placed FIRST, ahead of similarity hits, within
    # top_k. In a multi-step task the previous step is the dependency the next
    # question never names. Such rows carry score_components.recent_first = 1.
    recent_first: int = Field(default=0, ge=0, le=20)
    # Run-scoped anchor tier: the N OLDEST memories of this
    # identity — restricted to `run_id` when the request carries one — are
    # placed first, ahead of similarity hits, within top_k. The first thing a
    # task wrote is the frame every later step assumes (the base itinerary's
    # dates and origin; the problem statement) and a later question never
    # names it. Rows carry score_components.anchor_first = 1.
    anchor_first: int = Field(default=0, ge=0, le=20)
    # Per-speaker quota: each speaker in the fused pool is guaranteed up to this
    # many rows of the top_k before the remainder fills by rank, so one voice
    # of a two-party conversation cannot take the whole window. None or 0 =
    # off.
    per_speaker_quota: int | None = Field(default=None, ge=0, le=50)
    # Drop hits whose body is shorter than this many characters (0 = off).
    # A near-empty memory ("Initial result: Empty") can clear the noise floor
    # through the lexical leg and take a top-3 slot; the key alone is what
    # such a hit is worth.
    min_body_chars: int = Field(default=0, ge=0, le=4000)
    # Where the graph walk starts: "query" = the question's
    # entities and nearest triples (the default); "recent" = the entities
    # of this identity's newest memories (the session's when session_id is
    # set), so what was just talked about reaches the context whether or not
    # the question names it; "both" = the union. "" = the deployment default
    # (LOOM_MEMORY_SEARCH_GRAPH_SEED). Graph mode only; seeds, never a filter.
    graph_seed: Literal["", "query", "recent", "both"] = ""
    # Evidence completion. "session": at most
    # `diversify_session_cap` hits per source session in the block; the rest
    # of a session's rows yield their slot to the next session in rank order.
    # Without it, ranks 6–10 come from the same sessions again (Recall@5 62.5
    # → Recall@10 62.6 on multi-session conversations); spreading evidence across sessions is what
    # makes a larger k pay. "" or "none" = off.
    diversify: Literal["", "none", "session"] = ""
    # Per-session cap for diversify="session"; None = max(2, top_k // 3).
    diversify_session_cap: int | None = Field(default=None, ge=1, le=50)
    # At most this many digest rows (source summary / entity_summary) in the
    # final block; None or 0 = unbounded.
    summary_cap: int | None = Field(default=None, ge=0, le=50)
    # Fact rows first: the largest share of the block that verbatim chunk
    # rows may hold; surplus slots go to the best unreached fact rows. None or
    # 0 = off.
    chunk_cap_share: float | None = Field(default=None, ge=0.0, le=1.0)
    # What happens to hits under the similarity floor when top_k is set. None
    # = the deployment default (LOOM_MEMORY_SEARCH_FLOOR_MODE, "readmit").
    #   "readmit"  — appended after the confident rows, tagged below_floor, so
    #                the caller's k is filled (the default).
    #   "ids_only" — appended with an empty body: the key says a weak match
    #                exists without putting its text in the agent's context.
    #   "drop"     — removed; the response may carry fewer than top_k rows.
    # An off-topic prompt still retrieves k rows under "readmit" — the floor
    # only shapes confidence — which is why an extraction prompt gets bodies
    # back; ids_only/drop make the floor a bound, not a label.
    floor_mode: Literal["readmit", "ids_only", "drop"] | None = None
    # The similarity below which a hit is noise for THIS read.
    # None = the namespace's retrieval_defaults, else the calibrated floor.
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    # At most this share of the window is verbatim chunks (the chunk cap, per
    # read). None = namespace default, else
    # the deployment setting (1.0 = no cap).
    chunk_max_share: float | None = Field(default=None, ge=0.0, le=1.0)
    # Personalization arm. "full" = the default ranking. "none"
    # drops every user-derived rank term in every mode — preference boost,
    # discretionary prior group, reinforcement — and keeps relevance terms.
    # Per request, so the two arms can be compared on one server; the
    # score_components on each hit name what "full" added.
    personalization: Literal["full", "none"] = "full"
    # ISO YYYY-MM-DD anchor for resolving relative time in the query; when set the
    # resolved date is appended to the embedded query text. Empty = no expansion.
    observation_date: str = Field(default="", max_length=10)
    # Per-request reranker override. Empty = use the namespace's rerank_endpoint.
    # "none" = no rerank; "builtin:openai[:<model>]" / "builtin:cohere[:<model>]", or the
    # name of an endpoint in LOOM_RERANK_ENDPOINTS_JSON, reranks this call. A URL is refused.
    rerank: RerankName = ""
    # Cost posture. "standard" stages cheap recall first, spending expensive
    # LLM/graph/rerank legs only when the planner or fallback gates justify them.
    # "fast" makes no LLM call (as recipe "fast"); "deep" allows upfront expensive recall.
    retrieval_budget: Literal["fast", "standard", "deep"] = "standard"
    # HyDE: embed an LLM-generated hypothetical answer instead of the raw query to
    # close the question/stored-phrasing vocabulary gap. Left unset, True means
    # "allowed": standard-budget calls run cheap recall first and spend HyDE only
    # on deep/quality calls or a weak-result fallback. An explicit True opts in;
    # hyde=False forces vector-only.
    hyde: bool = True
    # As-of replay filter (coalesce(valid_at, created_at) <= as_of), denormalized
    # onto memory_embeddings so vector_search applies it without joining loom.memory.
    as_of: datetime | None = None
    # Which time axis `as_of` replays: "valid_time" (default; what was true as of
    # then) or "knowledge_time" (what loom had learned by then — created_at only,
    # ignoring valid_at; the incremental-ingest knowledge cutoff). No effect when
    # as_of is unset.
    as_of_basis: Literal["valid_time", "knowledge_time"] = "valid_time"
    # Internal: 1 inside a pass memory.search re-entered itself (the iterative retry, a
    # read-your-writes re-read after the first pass); such a pass never re-enters again.
    _pass_depth: int = 0
    # Internal: a pass (the iterative retry, loom_search's decisions pass) whose hits a
    # parent serves; it writes no trace, exposure or recall credit of its own.
    _merged_into_parent: bool = False
    # Shared cascade budget (_memory_search.SearchBudget) threaded through the
    # recursive fallback legs (iterative retry) so they share one
    # round/wall-clock bound. Private attr so it survives model_copy without
    # appearing in the schema.
    _search_budget: Any = None
    # Internal: the handler filled `search_mode` from the
    # deployment default — the caller pinned neither a mode nor a recipe.
    _search_mode_defaulted: bool = False
    # Internal: the knobs the namespace's `retrieval_defaults`
    # filled because the caller left them unset; rides `retrieval_plan`.
    _namespace_defaults_applied: tuple[str, ...] = ()
    # context.pack's per-section token budget, threaded down to govern the assembler
    # instead of the module default. 0 = default. Private attr; not on the wire.
    _context_pack_block_tokens: int = 0
    # Internal: the read makes no LLM call whatever its recipe (context.pack's
    # search), as the fast posture does, but keeps the recipe's recall width.
    _llm_free: bool = False


# The hit fields that ride only under `include_usage`.
_USAGE_FIELDS: tuple[str, ...] = (
    "reinforcement_count",
    "used_count",
    "used_cited_count",
    "harm_count",
    "evidence_count",
    "acted_count",
    "pinned",
)


class _MemorySearchHit(BaseModel):
    @model_serializer(mode="wrap")
    def _omit_unrequested(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # `tags` is on the wire only when the request asked for it: None = not
        # requested, so the key is dropped.
        data: dict[str, Any] = handler(self)
        if self.tags is None:
            data.pop("tags", None)
        # Same rule for the reliance counters (`include_usage`).
        for name in _USAGE_FIELDS:
            if getattr(self, name, None) is None:
                data.pop(name, None)
        # The per-hit abstain flag is on the wire only when it
        # is set.
        if self.below_threshold is None:
            data.pop("below_threshold", None)
        # `resolved_from` rides only on a hit seated for a
        # hidden duplicate.
        if self.resolved_from is None:
            data.pop("resolved_from", None)
        # The successor rides only on a superseded hit.
        for name in ("superseded_by", "superseded_at"):
            if getattr(self, name, None) is None:
                data.pop(name, None)
        # The fields the handler elided for this response
        # (`explanation` / `score_components` above the excerpt k without
        # `explain: true`). Empty for every response that pinned nothing.
        for name in self._elide:
            data.pop(name, None)
        return data

    # Keys dropped from the wire payload; private, set by the
    # handler, never in the schema. In-process readers still see the values.
    _elide: frozenset[str] = frozenset()
    # The stored row's tags as the leg that produced this hit
    # read them (every fact-table leg selects `tags`), private and never on
    # the wire. The post-fusion belt checks a hard `tags` filter against it
    # without a second read; None = the leg did not read them (point-read
    # only then). `tags` above stays the opt-in wire field.
    _row_tags: tuple[str, ...] | None = None
    # The stored row's agent and scope as the anchor pool read
    # them, private and never on the wire; the post-fusion belt checks the
    # request's agent against them without a read. None = the leg did not
    # read them (the belt leaves the row alone).
    _row_agent: str | None = None
    _row_scope: str | None = None
    # The stored memory's (agent, user_id, session_id) as the leg read them, private and
    # never on the wire: the code annotation reads that memory's links. None = not read.
    _row_identity: tuple[str, str, str] | None = None
    # The hit's code links in repositories the caller may read, as the search's code
    # annotation read them; None = not read (memory.ask reads them itself then).
    _code_links: list[dict[str, Any]] | None = None

    memory_key: str
    memory_type: MemoryType
    content_excerpt: str
    distance: float
    # The un-boosted score: `1 - distance`, the cosine of the
    # row to the query on every vector plane (fact, chunk, summary, …), in
    # [0, 1]. This is the number to threshold on. Every rank term — priors,
    # reinforcement, tag/entity/key-match boosts, the identifier lift — is
    # itemised in `score_components` and never added here.
    similarity: float
    # True when this row's `similarity` sits below the abstain
    # threshold the response's gate used (the `below_threshold` code's
    # threshold, namespace-calibrated). Off the wire when not set. The
    # response-level `abstain` reads the rank-1 row only; this flag reads
    # every row, so a caller can drop the tail of a k=30 window that the
    # gate did not see. Never set on a tier row (anchor_first / recent_first),
    # a derived / computed evidence row, or a record-plane row whose
    # `distance` is not a cosine.
    below_threshold: bool | None = None
    # The row's tags, present only under `include_tags=true`:
    # a list (possibly empty for chunk / digest / derived hits) when asked,
    # absent from the payload otherwise.
    tags: list[str] | None = None
    # Reliance counters, present only under `include_usage=true`; absent from the
    # payload otherwise. A chunk / digest / derived hit reads 0 / false.
    # `reinforcement_count` is the folded rank signal the fact leg reads; the others are
    # the usage ledger's sums for every live row of the key.
    reinforcement_count: int | None = None
    used_count: int | None = None
    used_cited_count: int | None = None
    harm_count: int | None = None
    evidence_count: int | None = None
    acted_count: int | None = None
    pinned: bool | None = None
    # Final blended rank score (post-boost, mode-dependent): the number the
    # served order follows, not a similarity. Under `rrf` it is the fused
    # 1/(k+rank) sum (≈ 0.03); under the cosine modes it is `similarity` plus
    # the itemised boosts, bounded to the pool (the identifier lift does not
    # add a flat 1.0). After an external reranker that changed the order it is
    # the pool's score for the rank the row now holds, its own in
    # score_components.rank_score_pre_rerank; a kept order changes no score.
    # For the per-component decomposition read score_components (explain=true).
    rank_score: float = 0.0
    # ISO datetime the fact is ABOUT (distinct from valid_at = when it became
    # true), surfaced directly for temporal reasoning. Empty when the fact has
    # no date dimension.
    temporal_anchor: str = ""
    # How precisely the source pinned temporal_anchor: 1.0 full date … 0.35
    # vague, per the extraction rubric; 0 = unscored.
    temporal_confidence: float = 0.0
    valid_at: str = ""
    # Graph mode: the chain from a query entity to one of this hit's
    # entities, seed first — ["alice"] when the hit names a query entity
    # itself, ["alice", "acme", "bob"] when it was reached by expansion.
    # [] in other modes and for hits ranked in for a non-graph reason.
    via_path: list[str] = Field(default_factory=list)
    # The row's own entities, when the request asked for them to be read
    # (entity coverage; the legs do not select the column, so
    # this is [] unless a coverage or boost step point-read it).
    entities: list[str] = Field(default_factory=list)
    # The identity the row was written under, filled with
    # `entities` by the pool point-read; "" when not read. A store that writes
    # each actor under its own user_id says "User's Day 2 dinner is …" in the
    # row, so the owner is the only thing that names the actor.
    user_id: str = ""
    # Retrieval layer: "fact" (loom.memory_embeddings) or "chunk" (verbatim,
    # loom.memory_chunks).
    source: str = "fact"
    # For a chunk hit: the memory key of the fact the chunk was written under
    # (`memory_chunks.parent_memory_key`, set on every memory.set chunk). A consumer
    # folds chunk hits onto their parent, credits the parent in run.complete and fetches
    # it with memory.get, without parsing the `chunk:<session>:<n>` id. "" for every
    # other plane.
    parent_memory_key: str = ""
    # Row state (active | superseded | duplicate | conflicted);
    # "" for chunks and summaries.
    status: str = ""
    # On a `status='superseded'` hit served under
    # `include_superseded`, the successor's memory_key (its id when that row
    # is gone) and the retirement instant. Off the wire otherwise.
    superseded_by: str | None = None
    superseded_at: str | None = None
    version: int = 0
    # Ingest time (ISO, loom.memory_embeddings.created_at); "" when unknown. The
    # arrival-order recency in memory.ask reads it.
    created_at: str = ""
    namespace: str = ""  # source namespace; populated on cross-namespace search
    # The hit's worst link to code ('' when it names no indexed code): `fresh`, `moved`,
    # `changed` (that code changed since the memory was written: check it), `unresolved`
    # (renamed or removed, and not followed: check it) or `missing`.
    code_state: CodeState | Literal[""] = ""
    # bodies="unit": true when the whole value did not fit the budget and
    # content_excerpt is still the clip; `next` is the one call that fetches it.
    body_truncated: bool = False
    next: str = ""
    source_resource_id: str = ""
    # Provenance class: asserted | extracted | observed |
    # derived | imported | external; '' when unrecorded.
    source_class: str = ""

    # Provenance: how the content entered memory and whether
    # an attestation is live — so a caller can filter on trust post-hoc.
    verified: bool = False
    # Compact agent-facing provenance so a hit can be cited without search_trace.
    explanation: str = ""
    # Provenance: the `session_id` the memory was written under; "" when the
    # row has none or the hit is a summary / derived / code row (a chunk or
    # record hit carries its row's). Lets a caller group hits by conversation
    # and a bench score source recall against gold evidence sessions without
    # a memory.get per hit.
    session_id: str = ""
    # Who said it, from the `speaker:<name>` tag; "" when the
    # fact is unattributed or cited to more than one speaker.
    speaker: str = ""
    # Whose fact this is: True for a row about someone other
    # than the user (`about:other`), so a caller can keep another person's
    # preference out of the user's personalisation.
    about_other: bool = False
    # For a digest hit (source="summary"): the memory keys it compressed;
    # empty for every other row.
    derived_from: list[str] = Field(default_factory=list)
    # Score breakdown of the ranking signals and, after a reranker, its moves (rerank_*).
    score_components: dict[str, float] = Field(default_factory=dict)
    # When this hit is a canonical seated in the slot of a
    # `duplicate` row the legs ranked, the duplicate's memory_key; absent
    # otherwise (None = not on the wire).
    resolved_from: str | None = None


class MemorySearchOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _omit_unset_disclosures(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # `reinforcement` is on the wire only when the read asked for a
        # recall credit: None (auto_touch off or nothing served) drops the key.
        data: dict[str, Any] = handler(self)
        if self.reinforcement is None:
            data.pop("reinforcement", None)
        return data

    # The settled input this search ran (None for a parked page); private, never on the wire.
    _settled_input: MemorySearchInput | None = None
    # The ranked hits, best first; empty when nothing passed the noise floor.
    results: list[_MemorySearchHit]
    # render="reader" only: the results as one prompt block ("[date] [key]
    # (type) excerpt", ordered as memory.ask would read them) and the
    # synthesizer's instructions, so an external reader scores what the ask
    # reader scores on the same hits.
    reader_block: str = ""
    # render="reader" only: the synthesizer instructions memory.ask pairs
    # with `reader_block`; empty otherwise.
    reader_instructions: str = ""
    # bodies="unit" accounting: whole values served / withheld for budget,
    # and the estimated tokens those bodies added (len/4).
    bodies_returned: int = 0
    # Hits whose whole value was withheld because `body_budget_tokens` ran out.
    bodies_elided: int = 0
    # Estimated tokens (chars / 4) the whole values served under bodies="unit" added.
    tokens_returned: int = 0
    # The namespaces in this search's scope: the addressed one first, then the
    # fan-out under namespace_scope=all_mine. A tiered all_mine search can stop
    # before it reaches the last of them. A single-namespace caller sees
    # exactly [input.namespace].
    namespaces_searched: list[str] = Field(default_factory=list)
    # Namespaces of the caller the fan-out did not read because it stops at
    # `memory_search_fanout_max_namespaces`; address one by name to search it.
    namespaces_skipped: list[str] = Field(default_factory=list)
    # Distinct memories in the fused pool before the top_k cut
    # ("showing 10 of 124"); the handle that serves the next slice when the
    # caller asked for paging and there was more; and this page's offset
    # into the parked list (0 for a fresh search).
    candidates_total: int = 0
    # Handle to pass as `page` for the next slice; empty when `paging` was off or no
    # more candidates remained.
    next_page: str = ""
    # This page's offset into the parked list; 0 for a fresh search.
    page_offset: int = 0
    # Share of the returned window that is verbatim chunks:
    # the reader for "did chunks crowd out the facts" on the ledger.
    chunk_share: float = 0.0
    # The embedding model the query was embedded with.
    embedding_model: str
    # Protocol nudge, set only when the call carried a run_id: how to report
    # which of these hits the agent relied on (run.complete memories_used).
    # A description is read once; a field on the response is read every call.
    next: str = ""
    # The identity's working-memory blocks, rendered as markdown, when the
    # call named a session_id or run_id and a block exists for that scope.
    # Run blocks first, then
    # the session's (memory.ask reads the run's blocks too; this is the
    # same scratchpad on the search response). Not ranked, not counted
    # against top_k: the agent's own notes for the task, re-read every call.
    scratchpad: str = ""
    # How many working-memory blocks `scratchpad` rendered; 0 = none.
    scratchpad_blocks: int = 0
    # Whether this read's recall credit was written.
    # `credited`, or `not_credited (measurement_label | no_consumer |
    # no_closer)`: a bench / probe label (`*`, `-`, `eval-*`, …), no named
    # consumer (the dashboard and the assistant toolset are none), or a consumer that has
    # not posted a closer (memory.outcome, memory.outcome_run, run.complete) in the window
    # and carries no run.
    # Absent when `auto_touch` was false or nothing was served.
    reinforcement: str | None = None
    # Graph-mode diagnostics, summed across the cross-namespace fan-out.
    # graph_seeds = 1-hop seed entities; graph_neighbors_added = neighbors
    # materialized from entity_edges and fed to rank fusion as graph_overlap.
    graph_seeds: int = 0
    # Neighbor memories pulled in from entity edges and fused as
    # graph overlap; 0 when graph expansion did not run or found none.
    graph_neighbors_added: int = 0
    # Relation-typing diagnostics: (s,v,o) triples pulled from the query, and the
    # deduped count of memory_keys with matching stored triples.
    triples_extracted_from_query: int = 0
    # Distinct memories whose stored triples matched a query triple.
    triples_matched: int = 0
    # How many instance phrases the set-question expansion
    # ran as extra legs (0 = trigger did not fire or the lever is off).
    set_expansion_phrases: int = 0
    # Abstention signal: True when retrieval can't honestly answer — results empty
    # after the noise floor, or top similarity below LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD.
    # Agents should respond with explicit absence rather than hallucinate.
    # The per-token ceiling that lowered this request's top_k;
    # 0 when none applied. Callers asking for more than their role allows see
    # fewer rows and this number, never an error.
    top_k_ceiling: int = 0
    # True when retrieval cannot honestly answer: no hit survived the noise floor,
    # the top similarity or calibrated confidence was too low, or a named entity is absent.
    abstain: bool = False
    # Human-readable reason for `abstain`; empty when it is False.
    abstain_reason: str = ""
    # Bounded machine-readable companion to `abstain_reason`. The prose reason
    # interpolates similarity values and the calibrator's free-text explanation,
    # so it is unbounded and MUST NOT be used as a metric label — that is what
    # this is for (see `loom.search.abstain` in observability/metrics).
    abstain_code: Literal[
        "",
        "no_match",
        "below_threshold",
        "low_calibrated_confidence",
        "entity_absent",
        # `page=<handle>` named a parked list that is gone.
        "page_expired",
    ] = ""
    # Providers that fell back on this read and what the response did instead
    # (`rerank: input_order_kept (ReadTimeout)`). A degraded answer is not the
    # normal answer, and this field is how an agent tells them apart.
    degraded: list[str] = []
    # Named recipe applied (if any) + a terse final scoring posture.
    recipe: str = ""
    # Terse scoring posture: mode, recipe, hyde, rerank and tag filter, then
    # the retrieval plan (`mode=rrf, hyde=on; plan=...`).
    score_explanation: str = ""
    # Calibrated confidence in the hit set, 0 to 1, from top similarity,
    # top-two margin, route agreement and result count; 0 on no hits.
    retrieval_confidence: float = 0.0
    # Risk label from `retrieval_confidence`: "low" (>= 0.75), "medium"
    # (>= 0.55), "high" (below), "abstain" (below the abstain thresholds).
    retrieval_risk: Literal["low", "medium", "high", "abstain"] = "abstain"
    # Share of the retrieval routes that ran (semantic, graph, triples, evidence,
    # extra legs) that also found support, 0 to 1.
    route_agreement: float = 0.0
    # The inputs behind `retrieval_confidence`: top_similarity, margin,
    # route_agreement, evidence_support (and constrained=true when set).
    calibration_explanation: str = ""
    # Recursive-fallback cascade accounting (iterative-judge). rounds_used =
    # rounds spent (capped at
    # LOOM_MEMORY_SEARCH_FALLBACK_MAX_ROUNDS); budget_exhausted = a leg was denied
    # by the round/wall-clock cap; elapsed_ms = wall-clock in the cascade.
    fallback_rounds_used: int = 0
    # True when a fallback leg was denied by the round or wall-clock cap.
    fallback_budget_exhausted: bool = False
    # Wall-clock milliseconds spent in the fallback cascade.
    fallback_elapsed_ms: int = 0
    # Planner diagnostics: pre-execution shape estimate + best-effort actual work.
    retrieval_plan: str = ""
    # Pre-execution work estimate: budget_class, llm_calls, embedding_calls,
    # vector / lexical / graph / structured queries, candidate_pool, top_k.
    retrieval_cost_estimate: dict[str, Any] = Field(default_factory=dict)
    # Work actually done: per-stage counters (e.g. `<stage>_added`), fallback
    # LLM and vector-query use against their budgets, and `llm_calls_total`.
    retrieval_work: dict[str, Any] = Field(default_factory=dict)
    # Deferred set_from_messages batches not yet extracted in the searched
    # scope. >0 means "a save is in flight", so an empty or
    # thin result is not evidence of absence; re-ask after the worker runs.
    pending_extraction: int = 0
    # Exact in-ClickHouse aggregate for derived-answer queries (aggregational /
    # temporal_diff) over per-occurrence rows — what the vector planes structurally
    # can't produce. None otherwise / on no match. Shape:
    #   {op, count, sum, avg, first_at, last_at, span_days, subject, predicate,
    #    category, items: [...]}
    # When populated it's ALSO mirrored as a synthetic first row in `results`
    # (source="derived", memory_key="derived_aggregate", rank_score above the pool
    # max), so results-only readers see the exact answer — the list may then hold
    # top_k + 1 entries.
    derived_aggregate: dict[str, Any] | None = None


# ============================================================
# memory.aggregate — predicate / aggregation queries over typed records
# ============================================================
#
# search answers "what's about X"; it can't answer "how many X" / "sum of Y"
# (embedding compression doesn't preserve predicate truth). aggregate does:
# deterministic SQL over JSON-extracted fields in loom.memory.value, scoped to
# the identity tuple. Pairs with set_from_messages(record_schema=...) which
# lands typed records; free-text rows are silently excluded (JSONExtract → 0).


class MemoryAggregateInput(_Identity):
    """Templated SQL aggregation over typed records in loom.memory.

    A high row is never counted, grouped or matched: its value is readable by key only.

    `agg_func` is whitelisted; field names pass through an identifier
    regex; all values bind as CH params. Hard LIMIT 1000 result rows.

    Example: total expenses by category for a user:
      agg_func="sum", agg_field="amount", group_by=["category"]
    """

    # Aggregate to compute: count (rows), or sum / avg / min / max of `agg_field`.
    agg_func: Literal["count", "sum", "avg", "min", "max"] = "count"
    # Field within value JSON to aggregate. Required for sum/avg/min/max; "*" for count.
    agg_field: str = "*"
    # Up to 3 value JSON field names to group by; empty = one row over all matches.
    group_by: list[str] = Field(default_factory=list, max_length=3)
    # Equality filter on JSON-extracted string fields; the identity tuple is always ANDed in.
    filter: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=100, ge=1, le=1000)  # SQL also hardcodes a LIMIT 1000 floor


class _AggregateRow(BaseModel):
    # group_by field values; empty dict when group_by was [].
    group: dict[str, str] = Field(default_factory=dict)
    metric: float  # float for sum/avg/min/max; int for count


class MemoryAggregateOutput(BaseModel):
    # One row per group (a single row when `group_by` is empty).
    rows: list[_AggregateRow] = Field(default_factory=list)
    sql_executed: str = ""  # the exact SQL run, for debugging without trace logs


# ============================================================
# memory.search_trace — per-candidate scoring breakdown
# ============================================================


class MemorySearchTraceInput(MemorySearchInput):
    """Same shape as memory.search; returns the vector leg's per-candidate scoring
    breakdown (no fusion, assembly or reranker).
    Why a served row ranked where it did: memory.search with explain=true."""


class _SearchTraceHit(BaseModel):
    memory_key: str
    memory_type: MemoryType
    content_excerpt: str
    distance: float
    similarity: float  # 1 - distance
    entity_boost: float = 0.0  # 0.5 if entity_filter intersects, else 0
    rank_score: float  # final blended score
    rank: int  # 1-indexed position after sort
    # Retrieval layer: "fact" (loom.memory_embeddings) or "chunk" (loom.memory_chunks).
    source: str = "fact"
    # The chunk's parent fact key (see _MemorySearchHit.parent_memory_key).
    parent_memory_key: str = ""


class MemorySearchTraceOutput(BaseModel):
    # Every candidate the fact leg scored, in the leg's rank order, with its breakdown.
    results: list[_SearchTraceHit]
    # The embedding model the query was embedded with.
    embedding_model: str
    # The request's `entity_filter`, lowercased as it was matched.
    entity_filter: list[str]
    # Query words embedded in the corpus's spelling instead of as typed
    # (`memory_search_query_spelling_max_words`); {} when none was.
    query_spellings: dict[str, str] = Field(default_factory=dict)
    # True when the query was embedded as typed because the spelling lookup
    # did not answer (failed, timed out, namespace backing off) or the
    # respelled text's embed failed.
    query_spellings_skipped: bool = False


# ============================================================
# memory.search_trace_log — read the per-query recall telemetry table
# ============================================================


class SearchTraceLogInput(BaseModel):
    """Read loom.search_trace_log for a namespace (requires the telemetry
    flag). Optional agent / run_id filters; time window in hours."""

    org: str
    namespace: str
    # Look-back window in hours, 1 to 8760 (one year).
    since_hours: int = Field(default=24, ge=1, le=8760)
    agent: str = ""
    # Only searches made under this run; "" or "-" = every run.
    run_id: str = ""
    # At most this many rows, newest first; capped at 1000.
    limit: int = Field(default=100, ge=1, le=1000)


class _SearchTraceLogRow(BaseModel):
    ts: datetime
    agent: str
    query: str
    returned_keys: list[str]
    returned_scores: list[float]
    top_k: int
    result_count: int
    abstain: bool
    retrieval_risk: str
    embedding_model: str
    latency_ms: int
    run_id: str
    # The wire op that ran the search — memory.search,
    # memory.ask, context.pack or the MCP search tool; '' when unrecorded.
    operation: str = ""


class SearchTraceLogOutput(BaseModel):
    # Recorded searches, newest first. A non-admin sees only its own principal's rows;
    # empty when trace logging is off.
    rows: list[_SearchTraceLogRow]
