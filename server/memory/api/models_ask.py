"""memory.ask: a question answered from the caller's recalled memories, with
the memory keys it cites."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    model_validator,
)

from server.memory.api.models_common import _Identity
from server.memory.api.models_search import _MemorySearchHit

# ============================================================
# memory.ask — grounded synthesis over recalled memories
# ============================================================
#
# search returns raw memories; context.pack formats sections; neither
# synthesizes. ask retrieves the top-k (exactly as memory.search would) and,
# unless disabled or the namespace is over its cost cap, has loom's configured
# LLM write a concise, specific answer grounded ONLY in those memories, with
# the memory_keys it used as citations. The MEMORY-plane mirror of code.ask.


class _RecentMessage(BaseModel):
    """One turn of the conversation a memory.ask question sits in. Not a memory: never
    written, never embedded, never searched."""

    role: str = "user"
    content: str = Field(min_length=1, max_length=6000)
    name: str = Field(default="", max_length=80)
    timestamp: str = Field(default="", max_length=40)


class MemoryAskInput(_Identity):
    """Ask a question and get a grounded answer synthesized from the caller's
    recalled memories (or, with allow_synthesis=False, just the memories)."""

    # The question to answer from the caller's memories (1 to 2000 chars).
    query: str = Field(min_length=1, max_length=2000)
    # Same semantics as memory.search: None = facts (reflections excluded);
    # "reflection" = ask over past runs' process lessons only.
    memory_type: (
        Literal["semantic", "procedural", "preference", "reflection", "decision"] | None
    ) = None
    # The same tag predicate memory.search applies, on the
    # candidate read this ask synthesizes from — hard OR-filter by default
    # (`hasAny(tags, …)`), a rank boost of `tag_boost` under `tags_soft`. So
    # "what did we learn about Storages" reads the subsystem's rows
    # (`tags: ["subsystem:Storages"]`), not the corpus. Empty = no filter.
    tags: list[str] = Field(default_factory=list, max_length=64)
    # True turns `tags` into a rank boost of `tag_boost` instead of a hard filter.
    tags_soft: bool = False
    # Forwarded only when set (None = memory.search's own default, 0.3).
    tag_boost: float | None = Field(default=None, ge=0.0, le=1.0)
    # 15: a recency-aware synthesis eval on an autonomous-agent recall workload
    # scored k=15 (with dated, trust-most-recent synthesis) at 57% vs 50% at
    # k=8 — the extra candidates give the synthesizer the fresh
    # fact even when stale cross-session copies rank alongside it.
    top_k: int = Field(default=15, ge=1, le=100)
    # Body mode of the retrieval leg: None keeps the
    # deployment default; "source" serves each fact hit its linked evidence
    # passage (or an N-character capsule with `source_span_chars`) so the
    # reader sees the turn or trace the fact came from. The candidate pool
    # read stays bodies="none" whatever is set here.
    bodies: Literal["full", "excerpt", "none", "unit", "source"] | None = None
    # Token budget for whole bodies on the retrieval leg; None = the
    # memory.search default. Only matters with `bodies` "unit" or "source".
    body_budget_tokens: int | None = Field(default=None, ge=200, le=32000)
    # bodies="source": 0 serves the whole linked passage, N an N-character
    # window around the extracted span. Forwarded only when > 0.
    source_span_chars: int = Field(default=0, ge=0, le=20000)
    # False = retrieval_only: return the memories and let the agent synthesize
    # (parity with code.ask's synthesize=False). True = server-side answer.
    allow_synthesis: bool = True
    # An evaluation read. False = the ask credits no usage
    # (no `used` / `contradicted` events for the rows it cites), so a
    # benchmark or replay does not feed the reinforcement term the next
    # ranking reads. True (default) = the agent's own read, credited.
    reinforce: bool = True
    # Per-request override of the decayed-reinforcement rank
    # weight (None → server default), forwarded to the retrieval legs.
    reinforcement_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    # Cap the synthesized answer length. 0 = unbounded (default). A recall
    # answer is injected into the agent's context, so a caller on a tight
    # budget can ask for a concise, question-shaped answer (a ~1k-char answer
    # beats a fixed-size dump); it scales max_tokens and
    # hard-truncates. Only affects synthesis, not the returned raw memories.
    max_answer_chars: int = Field(default=0, ge=0, le=8000)
    # Pass-through search controls, defaulting exactly as memory.search does:
    # None takes the deployment default `memory_search_default_namespace_scope`,
    # so an unscoped ask reads the same namespaces an unscoped search does.
    namespace_scope: Literal["this", "all_mine"] | None = None
    # Retrieval fusion mode, as memory.search takes it; forwarded only when set on
    # the request, otherwise the search's own default applies.
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
    # Read ALL agents in the accessible namespaces by default (membership is
    # the read boundary; agent is authorship metadata), matching memory.search,
    # and serve every bot's promoted rules. Set an explicit agent to narrow to
    # that one author's rows and that bot's rules. Over MCP an unnamed agent is
    # the client's own, so the read is narrowed to that agent; on the assistant
    # toolset, which writes nothing, it stays `*`.
    agent: str = "*"
    # Same semantics as memory.search.read_scope; binds to this op's user_id /
    # session_id identity fields. The token's claim is the ceiling.
    read_scope: Literal["namespace", "own_user", "own_session"] = "namespace"
    # Forwarded to memory.search unchanged: None = deployment
    # default demotion of `sensitivity:*`-tagged rows, zeroed on contact intent.
    sensitivity_penalty: float | None = Field(default=None, ge=0.0, le=1.0)
    # Digest rows admitted to the synthesis window; None = the
    # deployment setting, 0 = unbounded. Threads to the retrieval leg.
    summary_cap: int | None = Field(default=None, ge=0, le=50)
    # Forwarded to memory.search only when pinned.
    rrf_adaptive_lexical: bool | None = None
    # Same semantics as memory.search.personalization; the retrieval leg of an
    # ask runs with it so the answer can be compared with and without priors.
    personalization: Literal["full", "none"] = "full"
    # As-of temporal anchor. When set, retrieval is replay-filtered to memories
    # known by then (coalesce(valid_at, created_at) <= as_of, the same filter
    # memory.search applies) AND synthesis treats `as_of` as "now" — it reports
    # the state as of that instant and does not assert events dated after it.
    # This is what a time-anchored question needs ("status of PR X as of the
    # 3rd", reproducing a past decision) and it stops a corpus that already
    # holds later facts from leaking them into an answer about an earlier time.
    # Unset (the default) = answer from everything known.
    as_of: datetime | None = None
    # Which time axis `as_of` replays: "valid_time" (default) = state true as of
    # the anchor; "knowledge_time" = only what loom had learned by the anchor
    # (created_at cutoff, incremental-ingest knowledge-cutoff semantics). Use knowledge_time
    # for "what did we know at T" — it never grounds on a fact recorded later,
    # even one backdated to an earlier valid_at.
    as_of_basis: Literal["valid_time", "knowledge_time"] = "valid_time"
    # Forwarded to the retrieval leg; the retired rows then
    # stay in the synthesis window, each line labelled `[superseded <date>;
    # replaced by <key>]`, so the answer can say what a value was before and
    # what replaced it. Off (the default): retired rows leave the pool and the stale
    # sentences that restate them are masked.
    include_superseded: bool = False
    # Precedent reuse: serve the earlier synthesized answer to
    # this question+shape when the synthesis window is byte-identical (same
    # keys, same content hashes) to the one it was written from; no LLM call.
    # None = the server setting (LOOM_MEMORY_ASK_PRECEDENTS, default off);
    # False forces a fresh synthesis (benches), True opts in for this call.
    precedents: bool | None = None
    # Exact-prompt LLM response cache: every synthesis-path
    # call is read from loom.llm_response_cache by the sha256 of its full
    # prompt before the provider is called. The evidence is in the key, so a
    # changed memory misses by construction. None = the deployment default
    # (LOOM_MEMORY_ASK_LLM_CACHE_DEFAULT, scoped by
    # LOOM_MEMORY_ASK_LLM_CACHE_NAMESPACES); False forces provider calls (a
    # bench measuring synthesis itself), True opts in for this call.
    llm_cache: bool | None = None
    # Reading depth. "excerpt" answers from fact excerpts only (the default
    # behaviour). "raw" also opens the source turns behind the most relevant
    # facts (one selection call, a bounded evidence read, one more synthesis)
    # on every call. "auto" opens them for a temporal question and otherwise
    # answers from excerpts. Which depth answered rides the output as `depth`.
    depth: Literal["auto", "excerpt", "raw"] = "auto"
    # How the raw read chooses what to open: "llm" (one selection call over
    # the pool) or "top" (the top-ranked memories, no LLM — a deterministic
    # descent from the ranked facts to their source turns). None = the
    # deployment setting. Per request so the two can be A/B'd on one server.
    deep_select: Literal["llm", "top"] | None = None
    # When the deep pass is known upfront, synthesize once with the raw turns
    # (None/True) or run the excerpt synthesis beside the selector, then the
    # deep one (False). Per request so the two orders can be A/B'd on one
    # server.
    deep_first: bool | None = None
    # Server-side recount of a COUNT answer. None/True = on; False keeps the
    # model's own number for a caller whose judge prefers it; per request so
    # the two can be A/B'd on one server.
    recount: bool | None = None
    # Input budget for the synthesis window, in estimated tokens (at the prose
    # rate). None (default) = the fixed window of memory_ask_context_memories
    # excerpts, unchanged. When set, the ranked window is cut from the tail so
    # the memories the synthesizer reads fit the budget; the top memory is always
    # kept. What was seen and cut rides the output as context_tokens_est /
    # context_trimmed. Every other loom reader (context.pack, memory.stack,
    # code.ask) already takes such a budget.
    token_budget: int | None = Field(default=None, ge=256, le=32000)
    # A recommendation-shaped question ("can you suggest…", "what should I…")
    # is answered from the user's recorded preferences, tools and constraints
    # first, then tailored. None/True = on; False turns it off for this call,
    # so arms run on one server.
    personalize: bool | None = None
    # LLM-free window cut: keep synthesis rows whose cosine is
    # >= this fraction of the best row's (e.g. 0.75), never fewer than
    # LOOM_MEMORY_ASK_RELEVANCE_GATE_MIN_KEEP rows. None or 0 = no cut.
    # Reported via gate_mode="knee".
    window_rel_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    # Question decomposition: "" or "off" = none; "always" splits this
    # question into 2-4 sub-questions (the cheap tier) and retrieves each;
    # "auto" does so only on multi-hop or date-of-event cues. The
    # sub-questions ride the output.
    decompose: Literal["", "off", "auto", "always"] = ""
    # General-knowledge bridging: "" = the server setting
    # (LOOM_MEMORY_ASK_WORLD_KNOWLEDGE, default forbid); "allow" lets the
    # synthesizer combine a memory fact with public knowledge, marking the
    # sentences that did (output `grounding` = "mixed"); "forbid" keeps this
    # call memory-only.
    world_knowledge: Literal["", "forbid", "allow"] = ""
    # When the answer-vs-citation judge runs. "" = the server
    # setting (LOOM_MEMORY_ASK_CONSISTENCY_MODE, default inline); "inline"
    # awaits it and rides the verdict on the wire; "deferred" answers first —
    # the wire fields stay empty and the verdict lands on the ledger as a
    # `memory.ask.consistency` row instead. No effect while the check is off.
    consistency: Literal["", "inline", "deferred"] = ""
    # The verdict prior for the cheap-vs-strong router. "" = the
    # server setting (LOOM_MEMORY_ASK_SYNTH_ROUTE_PRIOR, default off); "off"
    # forces the rule-only router; "measured" the shipped table; or a JSON
    # object of route -> contradicted rate. `route_prior_max_cheap` pins the
    # rate at or under which a strong-routed ask goes cheap (None = setting).
    # Only matters when the cascade is on. The A/B lever — no restart.
    route_prior: str = Field(default="", max_length=2000)
    # Contradiction rate at or under which a strong-routed ask goes cheap;
    # None = the deployment setting.
    route_prior_max_cheap: float | None = Field(default=None, ge=0.0, le=1.0)
    # The mid synthesis tier, pinned per request. `mid_model`
    # "" = the server setting (LOOM_MEMORY_ASK_MID_MODEL, default none); "off"
    # = no mid tier for this call; a model name = that model. `mid_routes`
    # "" = the setting (LOOM_MEMORY_ASK_SYNTH_MID_ROUTES, default "uncertain");
    # "off", "uncertain" or a comma list of reason tokens. Per request so the
    # arms can be A/B'd on one server.
    mid_model: str = Field(default="", max_length=120)
    # Which routes use the mid tier: "" = the setting, "off", "uncertain" or a
    # comma list of route reason tokens.
    mid_routes: str = Field(default="", max_length=400)
    # The deployment default for the answer length, pinned per
    # request. None = the server setting (LOOM_MEMORY_ASK_DEFAULT_ANSWER_CHARS);
    # 0 = unbounded for this call; N = cap single-fact answers at about N
    # characters with the setting's exemptions (breadth and count questions).
    # Unlike `max_answer_chars` (which caps every shape and always wins), this
    # is the setting's semantics per call, so a bench can pair 0 against 1200
    # on one server.
    default_answer_chars: int | None = Field(default=None, ge=0, le=8000)
    # Search-side digest descent for this call (None = on, False = off).
    digest_descent: bool | None = None
    # Presupposition check in the synthesizer (None = on; False turns it off).
    presupposition_rule: bool | None = None
    # Synthesis window for set questions (None = setting, 0 = off).
    set_window: int | None = Field(default=None, ge=0, le=100)
    # Also read the knowledge plane (None = setting).
    knowledge: bool | None = None
    # The turns the question sits in, supplied by the caller
    # and never stored — rendered as a RECENT TURNS block above the evidence
    # so "and what did she say about that?" resolves against the turn above
    # it. Bounded here and again when rendered.
    recent_messages: list[_RecentMessage] = Field(default_factory=list, max_length=40)
    # The question's own date (ISO yyyy-mm-dd), as memory.search takes it:
    # "two months ago" resolves against it, for retrieval's date anchors and
    # for the synthesizer's note. Empty = today. Without it a replayed or
    # benchmarked question about "a couple of days ago" resolves against the
    # wall clock, possibly years off the corpus.
    observation_date: str = Field(default="", max_length=10)
    # Answer from loom's GitHub-items index when retrieval abstains on a
    # question naming a PR/issue number. None/True = on; False turns it off
    # for this call.
    gh_items_fallback: bool | None = None


class MemoryAskOutput(BaseModel):
    # The synthesized answer; empty when the ask abstained or ran retrieval_only.
    answer: str = ""
    # "record": answered from an exact record (the GitHub-items index or the
    # tracker) instead of LLM synthesis.
    # "precedent": the earlier synthesized answer to this question+shape,
    # served because its evidence is byte-identical.
    source: Literal["synthesized", "retrieval_only", "abstained", "record", "precedent"] = (
        "synthesized"
    )
    # True when retrieval abstained and no record answered: `answer` is empty.
    abstain: bool = False
    # Carries the retrieval abstain reason, or "budget_capped" when synthesis
    # was skipped because the namespace is over its cost cap.
    abstain_reason: str = ""
    # Citations: the memory_keys the answer drew on (all recalled keys in
    # retrieval_only mode).
    memory_keys: list[str] = Field(default_factory=list)
    # The raw retrieved memories, so callers still see the sources.
    results: list[_MemorySearchHit] = Field(default_factory=list)
    # The sessions the cited memories were written under: the `session_id` of every hit
    # in `results` whose key is in `memory_keys`, first-cited order, no repeats, ""
    # skipped. What a bench scores against gold evidence sessions per ANSWER and what an
    # agent groups a citation by.
    cited_sessions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_cited_sessions(self) -> MemoryAskOutput:
        if not self.cited_sessions and self.memory_keys and self.results:
            keys = set(self.memory_keys)
            seen: list[str] = []
            for h in self.results:
                sid = str(getattr(h, "session_id", "") or "")
                if h.memory_key in keys and sid and sid not in seen:
                    seen.append(sid)
            self.cited_sessions = seen
        return self

    # Protocol nudge, set only when the call carried a run_id (see
    # MemorySearchOutput.next).
    next: str = ""
    # Synthesis routing (two-tier cascade, LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP).
    # Which model produced the answer, which tier the policy chose, the rule
    # that chose it, and whether the cheap answer was escalated. Empty when no
    # synthesis ran; route_tier="strong"/reason="single_tier" when the cascade
    # is off, so an audit can tell "routed strong" from "no cheap model".
    synth_model: str = ""
    # The synthesis tier that answered: cheap, mid or strong; empty when nothing
    # was synthesized.
    route_tier: Literal["", "cheap", "mid", "strong"] = ""
    # The routing rule that chose `route_tier` ("single_tier" when the cascade
    # is off); empty when nothing was synthesized.
    route_reason: str = ""
    # How many of this ask's LLM calls the exact-prompt
    # response cache served (no provider call, nothing spent) and how many
    # went to the provider. Both 0 when the cache was off or nothing synthesized.
    llm_cache_hits: int = 0
    # LLM calls this ask sent to the provider past the response cache.
    llm_cache_misses: int = 0
    # Whether the cache wrapped this ask at all — the request
    # pin, the traffic class's policy or the deployment default, whichever
    # applied — and the dollars the hits did not spend at the price table
    # (the stored token counts of the served rows). `llm_cache=False` with
    # zero counts is "consulted nothing"; True with zero hits is a fill.
    llm_cache: bool = False
    # Dollars the cache hits did not spend, at the price table.
    llm_cache_saved_usd: float = 0.0
    # What the answer cited, scored without a model call
    # (`cited_sessions` above is the list these are computed on).
    # temporal_consistency: mean window score of the cited rows' dates against
    # the question's bounded window (None = no window / no dated citation);
    # citation_concentration: 1 / distinct cited sessions.
    temporal_consistency: float | None = None
    # 1 / distinct cited sessions; None when nothing was cited.
    citation_concentration: float | None = None
    # True when the cheap-tier answer failed its check and was redone on a
    # stronger tier.
    escalated: bool = False
    # Similarity-knee telemetry (`window_rel_floor`). gate_mode
    # is "" when the cut was off or skipped, else knee | floor | kept_all; the
    # counts say how many window rows the synthesizer actually read vs. dropped.
    gate_mode: str = ""
    # Window rows the synthesizer read after the similarity-knee cut.
    gate_kept: int = 0
    # Window rows the similarity-knee cut removed.
    gate_dropped: int = 0
    # Deferred batches not yet extracted in scope; see
    # MemorySearchOutput.pending_extraction.
    pending_extraction: int = 0
    # Raw turns of those pending batches that were read into the synthesis
    # window because they spoke to the question. > 0 says
    # the answer used evidence no memory row carries yet.
    pending_evidence: int = 0
    # The text retrieval actually embedded when the query carried an answer
    # scaffold (option block, "reply with the letter", stamped date) that was
    # split off for the synthesizer. Empty when nothing was
    # split — the common case.
    retrieval_query: str = ""
    # Facet completion: on a count question, the attribute
    # facets pulled (`age`, `location`) and how many in-scope memories they
    # added to the synthesis window beyond similarity retrieval.
    facets: list[str] = Field(default_factory=list)
    # In-scope memories the facet pull added to the window beyond
    # similarity retrieval; 0 when it did not run.
    facet_memories_added: int = 0
    # Answer-vs-citation audit (LOOM_MEMORY_ASK_CONSISTENCY_CHECK): does the answer say
    # what its cited memories say? "entailed" / "neutral" / "contradicted", "" when the
    # check did not run. consistency_score is (1 + P_entail - P_contradict) / 2.
    # consistency_note names the cited memory and phrase a contradiction turns on, so an
    # agent can decide to re-ask, verify or ignore.
    consistency: Literal["", "entailed", "neutral", "contradicted"] = ""
    # (1 + P_entail - P_contradict) / 2, 0 to 1; 0 when the check did not run.
    consistency_score: float = 0.0
    # The cited memory and phrase a contradiction turns on; empty otherwise.
    consistency_note: str = ""
    # Identifier chase: hops walked from the question's
    # identifiers through the identifiers the reached memories name, and the
    # memories those hops added to the window. 0 / 0 when the question named
    # no identifier or the reached memories named nothing new.
    identifier_chase_hops: int = 0
    # Memories the identifier-chase hops added to the window.
    identifier_chase_added: int = 0
    # The question's premise checked against PINNED or human-VERIFIED memories
    # in the window (LOOM_MEMORY_ASK_PREMISE_CHECK). When the
    # question presupposes something a trusted memory contradicts, the key of
    # that memory and a one-line note ("question presupposes: ...; [key]
    # records: ..."); the synthesizer was told to hold the trusted fact and
    # correct the premise. Empty when no conflict, no trusted rows, or off.
    premise_conflict: str = ""
    # One line naming the presupposition and what the trusted memory records;
    # empty when no conflict was found.
    premise_note: str = ""
    # The synthesizer's premise verdict (supported / unsupported / "").
    premise_verdict: str = ""
    # "fired" when the computed attribution note rode
    # the synthesis call, "" otherwise.
    attribution_note: str = ""
    # Reading depth that produced the answer: "excerpt" = fact excerpts only,
    # "raw" = the source turns behind the selected facts were opened too.
    # Empty when no synthesis ran. `deep_reason` says why the raw read ran
    # ("requested" or "temporal"),
    # `deep_keys` which memories were opened, `deep_chars` how much source
    # text the synthesizer saw.
    depth: Literal["", "excerpt", "raw"] = ""
    # True when the deep-evidence pass ran (see `deep_reason`), even if it opened
    # no source text.
    deep_fired: bool = False
    # The sub-questions retrieved on their own; empty when the
    # question was retrieved whole.
    decomposed: list[str] = Field(default_factory=list)
    # How many sub-questions that was (0 = retrieved whole). The list above is
    # text and never reaches the ledger; this count rides it as
    # out_decomposed_parts, so the arm's firing rate is one GROUP BY away.
    decomposed_parts: int = 0
    # Superseded rows taken out of the synthesis pool; 0 under
    # `include_superseded`.
    superseded_dropped: int = 0
    # Sentences in chunks and summaries masked because they restate a
    # superseded row.
    stale_sentences_masked: int = 0
    # Bridge entities the second hop followed.
    entity_followed: list[str] = Field(default_factory=list)
    # Dated raw turns the second synthesis read (0 = no fallback).
    when_evidence_turns: int = 0
    # Sub-question retrievals that raised and were skipped
    # (fail-open), and the widened pool the retrieval ran with (0 = the
    # caller's top_k). Both ride the ledger as out_decompose_failed /
    # out_pool_k so an inert lever shows up in one GROUP BY.
    decompose_failed: int = 0
    # The widened candidate pool retrieval ran with; 0 = the caller's `top_k`.
    pool_k: int = 0
    # "memory": every sentence came from the memories; "mixed": the answer
    # bridged with general knowledge under world_knowledge="allow" and marked
    # those sentences; "": no synthesis ran.
    grounding: Literal["", "memory", "mixed"] = ""
    # Why the raw read ran: "requested" (depth="raw") or "temporal"; empty
    # when it did not.
    deep_reason: str = ""
    # The memory keys whose source turns the raw read opened.
    deep_keys: list[str] = Field(default_factory=list)
    # Characters of source text the raw read handed the synthesizer.
    deep_chars: int = 0
    # Code-plane grounding rows the synthesizer saw.
    code_hits: int = 0
    # Fact rows and verbatim chunk rows the synthesizer saw.
    pool_facts: int = 0
    # Verbatim chunk rows the synthesizer saw.
    pool_chunks: int = 0
    # Estimated tokens per block of the synthesis prompt —
    # system, notes, window (fact and summary rows), chunks, code, chase,
    # knowledge, raw_turns, timeline, recent, scratchpad, elision, graph,
    # question, other (heads and separators) — summed over the ask's synthesis-family calls. The
    # estimate is the listing budget's chars/4; the provider reports one
    # input total per call (`synth_input_tokens`), never per block. Blocks
    # that rendered nothing are absent; empty when nothing was synthesized.
    prompt_blocks: dict[str, int] = Field(default_factory=dict)
    # Which selector chose the opened memories: "llm" or "top" ("" when the
    # raw read did not run).
    deep_selector: Literal["", "llm", "top"] = ""
    # How the deep-first order resolved. "single" = one
    # synthesis with the raw turns (no excerpt synthesis ran); "empty_block" =
    # the raw block was empty so the excerpt synthesis ran alone;
    # "deep_failed" = the deep answer failed the citation contract, the
    # excerpt synthesis ran and the fallback rule chose between them; "" = the order
    # was not used (setting off, cheap tier, or no upfront trigger).
    deep_first: Literal["", "single", "empty_block", "deep_failed"] = ""
    # How many synthesis-family calls the ask made (primary, deep, escalation,
    # fallbacks; 0 when nothing was synthesized).
    synth_calls: int = 0
    # The provider-side accounting of the model that answered (`synth_model`)
    # over this ask: its calls, whole-prompt input tokens, the share the
    # provider served from its prompt cache and the share it wrote to it, and
    # its dollars at the price table (cache rates applied).
    # All zero when the answer came from the exact-prompt response cache.
    synth_input_tokens: int = 0
    # Input tokens of the answering model the provider served from
    # its prompt cache.
    synth_cached_input_tokens: int = 0
    # Input tokens of the answering model the provider wrote to its
    # prompt cache.
    synth_cache_write_tokens: int = 0
    # Dollars the answering model's calls cost at the price table, cache rates
    # applied.
    synth_cost_usd: float = 0.0
    # Estimated input tokens of the memories the synthesizer saw, and how many
    # ranked memories `token_budget` cut from the window (0 when no budget).
    context_tokens_est: int = 0
    # Ranked memories `token_budget` cut from the window; 0 when no budget.
    context_trimmed: int = 0
    # Session-summary hits that survived into the synthesis window.
    summary_hits: int = 0
    # Knowledge passages the synthesizer was shown.
    knowledge_hits: int = 0
    # Dated memories rendered in the computed timeline; 0 = none.
    timeline_rows: int = 0
    # A COUNT answer whose number was rewritten from the model's own ITEMS
    # verdicts.
    count_recomputed: bool = False
