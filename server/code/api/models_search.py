"""code.search (hybrid, shape-routed, calibrated abstention) and code.ask (the
cross-agent grounded answer cache): input and output models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from server.code.api.models_common import (
    _CodeIdentity,
    _drop_empty_lessons,
    _lead_namespaces,
    _LessonsOut,
    _ResultLessons,
    _trim_namespaces,
)
from server.code.domain.units import CodeUnit
from server.memory.domain.rerank_names import RerankName
from server.operations.base import MCP_HIDDEN

# ============================================================
# code.search
# ============================================================


# Longest code.search query kept; longer text is clipped, not refused.
QUERY_MAX_CHARS = 20000


class CodeSearchInput(_CodeIdentity, _ResultLessons):
    # The question, identifier or phrase to search for; a symbol-shaped query tries the
    # symbol table first (`route` says which path answered). A long one (a pasted issue
    # body) is clipped to QUERY_MAX_CHARS, never refused: the search keeps its rarest
    # terms (code_search_query_max_tokens) whatever its length.
    query: str = Field(min_length=1)

    @field_validator("query", mode="before")
    @classmethod
    def _clip_query(cls, v: Any) -> Any:
        return v[:QUERY_MAX_CHARS] if isinstance(v, str) else v

    # At most this many results per page (1-25).
    top_k: int = Field(default=5, ge=1, le=25)
    # Response budget in tokens; results are whole units, overflow elides.
    # None = route-aware default: 3000 for hybrid retrieval, 1200 for a
    # symbol-shaped query. Explicit values are honored unchanged.
    token_budget: int | None = Field(default=None, ge=200, le=12000)
    # Keep only units whose path starts with this prefix; "" = the whole namespace. A prefix
    # outside the indexed prefixes abstains with `abstain_code=not_covered`.
    path_prefix: str = ""
    # Continuation: the `next_cursor` of an earlier response to this query. The next
    # page is a point read of the pool that search ranked — no embedding call, no leg
    # scans. The response discloses `as_of.page_offset`, `page_commit`, `page_stale`
    # (the index moved) and `page_fallback` when the pool row was gone and a re-search
    # was run. A cursor for another query, prefix or ranking setting is a 400.
    cursor: str = Field(default="", max_length=1024)
    # The caller's seen-set: unit ids not to serve again. Dropped from the
    # fused list before diversification on a computed search; skipped while
    # walking the pool on a continuation page.
    exclude_unit_ids: list[str] = Field(
        default_factory=list, max_length=200, json_schema_extra=MCP_HIDDEN
    )
    # Reranker over the fused pool: "" = the deployment default
    # (LOOM_CODE_SEARCH_RERANK_DEFAULT, for prose questions of its kinds), "none" = off
    # for this request, "builtin:openai" / "builtin:cohere", or the name of an endpoint
    # in LOOM_RERANK_ENDPOINTS_JSON. A URL is refused.
    rerank: RerankName = Field(default="", json_schema_extra=MCP_HIDDEN)
    # Vocabulary bridge: one LLM hop proposes codebase identifiers for the
    # lexical leg (natural-language wording often misses the implementing
    # subsystem's vocabulary). Opt-in — adds ~500ms + one mini call.
    expand: bool = False
    # Risk annotations + one-hop callee context per result (churn/amp/tested/
    # callees on each unit). Costs a reserved slice of token_budget and 3-4
    # extra CH reads; LOOM_CODE_RISK_ANNOTATIONS kills it deployment-wide.
    annotate: bool = Field(default=True, json_schema_extra=MCP_HIDDEN)
    # "auto" (default): granularity scales with budget-per-slot — cards
    # when the budget can't fit any body (~<200 tok/slot), whole units in
    # the normal range, and top hits expand toward their WHOLE FILE when
    # the budget is generous. "none": cards always — the survey mode; fetch
    # a body via code.symbol when one card wins.
    # None = the deployment default (settings.code_search_bodies_default).
    bodies: Literal["auto", "none"] | None = None
    # Chunk-kind prior override for A/B: None = the served "flat" table; "default" /
    # "flat" pick a table, "structural" applies only the phrasing rule, "off" disables.
    kind_prior: Literal["off", "default", "flat", "structural"] | None = Field(
        default=None, json_schema_extra=MCP_HIDDEN
    )
    # Only PRs merged strictly before this instant may vote — the time-travel
    # pin a bench mined from merged PRs needs so the fix PR cannot vote for
    # its own files. None = all history.
    pr_leg_before: datetime | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # Bench-only override of the embedding_model label the legs read: "" = the
    # deployment embedder's model name. A twin label (`<model>+ctx2`) written beside the
    # live rows occupies its own ORDER BY range; this reads it for a paired A/B without
    # a redeploy. A request field only — served defaults never change. Keyed into the
    # memo and the cursor like the model name it replaces.
    embedding_model: str = Field(default="", max_length=128, json_schema_extra=MCP_HIDDEN)
    # true = bypass the served-response memo and compute.
    fresh: bool = Field(default=False, json_schema_extra=MCP_HIDDEN)
    # Symbol fan-in (loom.code_symbol_stats, weekly). Every unit carries `fan_in`
    # (non-test call sites reaching its symbol) when the stats are enabled. min_fan_in
    # drops units below the floor; sort_by='fan_in' orders the pool by dependents and,
    # with a path_prefix, adds the prefix's most-called symbols as candidates ("which
    # symbol under src/Interpreters has the most callers").
    min_fan_in: int = Field(default=0, ge=0)
    # "score" = ranked relevance order; "fan_in" = order the pool by `fan_in`, most-called first.
    sort_by: Literal["score", "fan_in"] = "score"
    # Constrained search: a literal (default) or RE2 regex that every returned chunk
    # must contain — applied to BOTH legs' pools before ranking, so one call replaces
    # grep-then-search. Case-insensitive by default (a question rarely spells the
    # identifier's case). A pattern matching more than `code_search_pattern_max_files`
    # files is refused with abstain_code=pattern_too_broad and the count: narrow it, or
    # add path filters. The symbol route is skipped when a pattern is set.
    pattern: str = Field(default="", max_length=500)
    # true = `pattern` is an RE2 regex (an invalid one is a 422); false = a literal substring.
    regex: bool = False
    # true = `pattern` matches case-sensitively; false (default) ignores case.
    case_sensitive: bool = False
    # Indexed-route file filters, rg `-g` / `-t` style: keep paths matching
    # any glob (`*.cpp`, `src/Parsers/**`), drop paths matching any exclude,
    # restrict to a language's extension set (cpp, python, sql, ...). Each
    # list at most 8. Applied to both legs (and code.grep) in plain WHERE.
    path_globs: list[str] = Field(default_factory=list, max_length=8)
    # Drop paths matching any of these globs (same syntax as path_globs); at most 8.
    exclude_globs: list[str] = Field(default_factory=list, max_length=8)
    # Keep only files of these languages by extension (cpp, c, python/py, js, ts, go, rust,
    # java, sql, md, ...); an unknown name is a 422 listing the known ones. At most 8.
    langs: list[str] = Field(default_factory=list, max_length=8)
    # Leg ablation for A/B: None/"hybrid" = both legs; "lexical" skips the vector leg
    # AND the embedding call; "vector" skips the lexical leg. A per-request field only —
    # there is no deployment setting, served defaults do not change. Routes read
    # lexical_only / lexical_expand / vector_only so the trace tells the arms apart.
    legs: Literal["hybrid", "lexical", "vector"] | None = Field(
        default=None, json_schema_extra=MCP_HIDDEN
    )
    # Developer diagnostic, not exposed to the shunt: the path of the file the caller
    # EXPECTED to see. The response's `tracked` block says where that file's chunks
    # stood in each leg's pool under the same tokens, idfs and query vector this search
    # used. One extra read under the (org, namespace, embedding_model, path) key prefix;
    # the request bypasses the served-response memo.
    track_path: str = Field(default="", max_length=1024, json_schema_extra=MCP_HIDDEN)
    # Second vector model: the spec of an embedder the deployment lists in
    # LOOM_CODE_SEARCH_VECTOR_MODELS. The vector leg embeds with it and reads the chunk
    # rows under its label; the lexical leg, anchors and bodies stay on the served
    # model. An unlisted spec is refused. A per-request A/B field — there is no served
    # default.
    vector_model: str = Field(default="", max_length=120, json_schema_extra=MCP_HIDDEN)
    # With vector_model set: > 0 = the second model shortlists this many chunk ids and
    # the served model's vectors rescore them exactly; 0 = the second model's own
    # distances rank the pool; None = the deployment's code_search_vector_shortlist.
    vector_shortlist: int | None = Field(default=None, ge=0, le=5000, json_schema_extra=MCP_HIDDEN)
    # `none` skips the model-backed legs — HyDE (an LLM passage, a second embed and a
    # full scan) and a model-named concept source; `auto` (None
    # = the deployment default, LOOM_CODE_SEARCH_LLM_LEGS_DEFAULT) runs what the
    # deployment configures.
    llm_legs: Literal["auto", "none"] | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # The request's wall-clock budget in ms, from arrival (values under 200 read as 200).
    # The legs run until what is left pays for ranking and the page; a leg still running
    # then is dropped and named in `legs_timed_out`, and optional late stages (rerank, the
    # whole-file escalation) are skipped when the budget is short. The answer is the
    # best-so-far page with `partial: true` and `partial_reason`; it never times out.
    # None = LOOM_CODE_SEARCH_DEADLINE_MS; 0 = none.
    deadline_ms: int | None = Field(default=None, ge=0, le=60000)
    # Several namespaces at once: the single-namespace search runs once per entry,
    # concurrently, and the rankings are interleaved by RANK in this order — scores are
    # not comparable across namespaces (the lexical idf is per namespace). Every entry
    # is authorized at VIEWER before anything runs; one refusal refuses the request. Not
    # combinable with `cursor` (a continuation pages ONE ranked pool). The served list
    # is re-budgeted to the request's token budget and a namespace past
    # `code_search_fanout_timeout_ms` is dropped and named in `fanout.dropped`.
    # `namespace`, when set and absent here, leads the list. Unset = one namespace and
    # no fan-out keys on the wire.
    namespaces: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("namespaces")
    @classmethod
    def _namespaces_trimmed_and_unique(cls, v: list[str]) -> list[str]:
        return _trim_namespaces(v)

    @model_validator(mode="after")
    def _namespace_leads_the_list(self) -> CodeSearchInput:
        self.namespaces = _lead_namespaces(self.namespace, self.namespaces)
        return self


class CodeSearchOutput(_LessonsOut):
    @model_serializer(mode="wrap")
    def _drop_empty_fanout(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # fanout, legs_timed_out and epistemic are on the wire only when they
        # fired, so a plain single-namespace response carries none of them
        data: dict[str, Any] = handler(self)
        for name in ("fanout", "legs_timed_out", "epistemic", "partial", "partial_reason"):
            if not data.get(name):
                data.pop(name, None)
        return _drop_empty_lessons(data)

    # The ranked units served on this page, cut to top_k and the token budget; empty on
    # an abstain with no pool or a not-covered path.
    results: list[CodeUnit]
    route: str  # symbol_exact | hybrid | symbol_miss_hybrid
    # Continuation: opaque cursor for the next page of the ranked pool, ""
    # when nothing ranked remains past this page (symbol routes issue none).
    # has_more mirrors it.
    next_cursor: str = ""
    # true when `next_cursor` is set, i.e. more ranked results remain past this page.
    has_more: bool = False
    # History section: {intent: number|phrase, numbers, items: [{number, kind, state,
    # title, ...}], prs: [{pr_number, title, author, merged_at, files, files_total,
    # role}], anchored: [{path, placed}], tokens}. Empty unless the history intent
    # fired.
    history: dict[str, Any] = Field(default_factory=dict)
    # true = the server does not stand behind these results (they may still be listed);
    # `abstain_code` says why.
    abstain: bool = False
    # Why it abstained: no_match (nothing survived) | below_threshold (top cosine under the
    # abstain threshold) | low_calibrated_confidence | not_covered (path outside the index) |
    # pattern_too_broad | identifier_absent (no file names it) | off_domain (no file card is
    # close to the question); "" when not abstaining.
    abstain_code: Literal[
        "",
        "no_match",
        "below_threshold",
        "low_calibrated_confidence",
        "not_covered",
        "pattern_too_broad",
        "identifier_absent",
        "off_domain",
    ] = ""
    # Human-readable reason for the abstain, with what to do instead; "" when not abstaining.
    abstain_reason: str = ""
    # Calibrated risk band from `retrieval_confidence`: low (>= 0.75), medium (>= 0.55),
    # high, or abstain (below the abstain cut-off or the top cosine under its threshold).
    retrieval_risk: Literal["low", "medium", "high", "abstain"] = "low"
    # Calibrated confidence 0-1 from the top similarity, its margin over the second hit,
    # `route_agreement` and the pool size; 0.0 on an abstain with no results.
    retrieval_confidence: float = 0.0
    # Share (0-1) of the retrieval routes that ran and agreed: the primary hit, lexical and
    # vector legs sharing a top-10 chunk, a fuzzy-symbol hit; 1.0 on an exact symbol hit.
    route_agreement: float = 0.0
    # The calibration inputs as text (top_similarity, margin, route_agreement, ...), plus
    # the legs the answer ranked without past the deadline.
    calibration_explanation: str = ""
    # Estimated tokens of the served page (results, history section and annotations).
    tokens_returned: int = 0
    # Constrained search: true when a pattern / path filter narrowed the pool;
    # `matched_files` is the pre-count of files the constraint admits (0 when
    # unconstrained or not counted).
    constrained: bool = False
    # How many files the pattern / path constraint admits; 0 when unconstrained or not counted.
    matched_files: int = 0
    # Provenance + freshness: branch, commit, indexed_at, lag_commits (int, -1
    # unknown), stale (bool: lag > LOOM_CODE_STALE_AFTER_COMMITS).
    as_of: dict[str, Any] = Field(default_factory=dict)
    # How the answer was reached as text: fusion kind and leg row counts, legs agreement,
    # the constraint and matched_files, priors and history votes that applied.
    score_explanation: str = ""
    # Counters per stage, plus `risk_source` — "table" | "live" | "mixed": where the
    # served risk block came from when the precomputed table is on.
    retrieval_work: dict[str, int | str] = Field(default_factory=dict)
    # Id of the loom.code_search_trace row the server writes for this call.
    trace_run_id: str = ""
    # Raw cosine of the top vector-leg hit (0.0 when the vector leg did not
    # run or the route was symbol_exact). Hits carry no cosine plane of their
    # own, so this is what the retrieval-quality metrics read for code.search.
    top_similarity: float = 0.0
    # track_path diagnostic: {path, verdict, chunks, pool, best: {chunk_id, scope,
    # start_line, lexical_rank, vector_rank, lex_score, distance, served_rank},
    # lexical_floor, vector_ceiling}. Empty unless the request set track_path.
    tracked: dict[str, Any] = Field(default_factory=dict)
    # Fan-out disclosure: {namespaces, interleave: "rank", timeout_ms, per_namespace:
    # {ns: {ms, results, abstain, route, commit, trace_run_id}}, dropped: [ns...]}.
    # Empty (and off the wire) unless the request named several namespaces.
    fanout: dict[str, Any] = Field(default_factory=dict)
    # e.g. "lexical only until embeddings finish (41% embedded)" while the namespace's
    # vector leg covers less than LOOM_CODE_READY_EMBED_COVERAGE of its chunks (a first
    # index still embedding, a backfill without --embed); "" otherwise.
    note: str = ""
    # The legs the deadline (or the model-leg grace) dropped — their rows are not in the
    # ranking — and `epistemic` "partial" then, or when a content leg failed
    # (`retrieval_work` names it). Both are off the wire when every leg that ran answered.
    legs_timed_out: list[str] = Field(default_factory=list)
    # "partial" when a leg timed out or a content leg failed; off the wire otherwise.
    epistemic: Literal["", "partial"] = ""
    # `deadline_ms` cut something (a leg, rerank, a late stage): the page is the best
    # found in the budget, and `partial_reason` names what did not finish. Off the wire
    # otherwise.
    partial: bool = False
    # What the deadline cut, as `deadline N ms: <stage>; <stage>`; off the wire otherwise.
    partial_reason: str = ""


# ============================================================
# code.ask (cross-agent grounded answer cache)
# ============================================================


class CodeAskInput(_CodeIdentity):
    # The question to answer about the indexed code (grounded on a code.search of it).
    question: str = Field(min_length=1, max_length=2000)
    # False = retrieval-only (agent synthesizes); True = grounded server-side
    # answer with citations, memoized across agents and invalidated when any
    # cited file changes at merge.
    synthesize: bool = True
    # Reranker for the retrieval: "" = the deployment default
    # (LOOM_CODE_SEARCH_RERANK_DEFAULT), "none" = off for this request, "builtin:openai" /
    # "builtin:cohere", or the name of an endpoint in LOOM_RERANK_ENDPOINTS_JSON (not a URL).
    rerank: RerankName = ""
    # Skip the cache lookup and synthesize fresh (the fresh answer still
    # refreshes the cache entry). For eval and for agents that suspect a
    # stale answer. Also skips the exact-prompt cache of the pre-synthesis
    # LLM calls.
    no_cache: bool = False
    # Retrieval context handed to synthesis, in tokens. None = the
    # deployment default (settings.code_ask_token_budget).
    token_budget: int | None = Field(default=None, ge=200, le=12000)


class CodeAskOutput(BaseModel):
    # The grounded answer text; "" on an abstain or when synthesize=false.
    answer: str = ""
    # The units the answer used (or was shown when none is cited): {path, start_line,
    # end_line, symbol, unit_id} each; the retrieved units when synthesize=false.
    citations: list[dict[str, Any]] = Field(default_factory=list)
    # cached = served from the answer cache | synthesized = answered now (or retrieval-only
    # when synthesize=false) | abstained = no grounded answer (`abstain_reason` says why).
    source: Literal["cached", "synthesized", "abstained"]
    # Cosine between this question and the served cached question; 0.0 unless source=cached.
    cache_similarity: float = 0.0
    # What the answer cache did. hit (a cached answer whose key, identifiers and files
    # all matched) | miss | stale (the nearest entry's files, prompt version or age
    # invalidated it) | guarded (the nearest cached question names different
    # identifiers) | key_mismatch (cached under another scope / budget / rerank / model)
    # | negative (this exact question abstained at this index head within the hour) |
    # reused_from:<ns> (a release branch served its trunk's entry, files identical) |
    # bypassed (no_cache, or no resolved code namespace).
    cache: str = "miss"
    # On a served entry: the synthesis wall (ms) and upstream cost (USD) the
    # entry recorded when it was written, i.e. what this call did not pay.
    cache_saved_ms: int = 0
    # Upstream LLM cost in USD the served cache entry recorded; 0.0 when nothing was served.
    cache_saved_usd: float = 0.0
    # true when no grounded answer is returned; `abstain_reason` says why.
    abstain: bool = False
    # Why it abstained (no strong match, off-repository question, ...); "" otherwise.
    abstain_reason: str = ""
    # The underlying code.search's calibrated confidence (0-1); 0.0 when it did not run.
    retrieval_confidence: float = 0.0
    # Estimated tokens of the answer text (characters / 4); 0 when there is no answer.
    tokens_returned: int = 0
    # Retrieval context (tokens) the synthesis was grounded on; 0 for cached hits and
    # abstains.
    context_tokens: int = 0
    # Id of the loom.code_search_trace row the server writes for this call.
    trace_run_id: str = ""
    # Provenance + freshness: branch, commit, indexed_at, lag_commits (int, -1
    # unknown), stale (bool: lag > LOOM_CODE_STALE_AFTER_COMMITS).
    as_of: dict[str, Any] = Field(default_factory=dict)
