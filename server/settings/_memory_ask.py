"""memory.ask: evidence assembly and answer synthesis."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.settings._env import CaseFolded, RoutePrior


class MemoryAskSettings(BaseModel):
    # memory.ask only: re-rank the synthesis window by similarity + weight * recency,
    # recency = 1/(1 + age_days/14), so the freshest copy of a fact reaches synthesis. 0.0 =
    # cosine only.
    memory_ask_recency_weight: float = 0.2

    # Fraction of the namespace's 24 h cost cap at which the cascade routes every ask cheap,
    # with no escalation. 0 = off.
    memory_ask_synth_budget_cheap_ratio: float = 0.7
    # Adaptive synthesis output budget: max(window rule, ratio * evidence tokens), capped at
    # memory_ask_synth_max_tokens_ceiling. The window rule (600 tokens at <= 15 memories,
    # 900 above) is the floor; 0 = the window rule alone.
    memory_ask_synth_budget_ratio: float = 0.75
    # Upper bound on the adaptive budget (a cost and p99 guard, not a target); the
    # provider's own maximum still applies.
    memory_ask_synth_max_tokens_ceiling: int = Field(default=8000, ge=1)
    # Fixed synthesis output-token budget; 0 = adaptive.
    memory_ask_synth_max_tokens: int = 0
    # The cheap tier answers only when the synthesis window holds at most this many evidence
    # tokens (chars/4); above it the ask routes strong (cheap losses averaged 5.9k tokens).
    # 0 = off.
    memory_ask_synth_cheap_max_evidence_tokens: int = 2500
    # Routing prior from the consistency judge's contradicted rate per route. '' = rule-only
    # router; "measured" = synth_policy.MEASURED_ROUTE_PRIOR; or a JSON object route ->
    # rate. A strong-routed ask whose route rate is <=
    # memory_ask_synth_route_prior_max_cheap goes cheap (reason "<rule>:prior_cheap"); hard
    # stops (identifier, breadth, rich evidence, as_of, ungrounded) never move.
    memory_ask_synth_route_prior: Annotated[str, RoutePrior] = ""
    # Route contradicted rate at or below which the routing prior sends a strong-routed ask cheap.
    memory_ask_synth_route_prior_max_cheap: float = 0.10
    # Which strong-routed reasons go to the mid tier: "uncertain", "off", or a comma list of
    # reason tokens.
    memory_ask_synth_mid_routes: str = "uncertain"

    # How many top memories the synthesizer sees, independent of the caller's top_k; also
    # sizes the fetch pool and, above 15, the answer-token budget. Raise it only with a
    # strong memory_ask_synth_model, since a weak one dilutes over more items.
    memory_ask_context_memories: int = Field(default=15, ge=1)
    # Deep-evidence pass: a selector picks up to memory_ask_deep_evidence_max_memories
    # ranked candidates, their source turns are read (loom.memory_evidence via
    # loom.memory_belief_sources, scoped rows first) and synthesis reruns with the raw
    # turns. Runs on depth="raw" and temporal questions. This field: how many candidates
    # the selector sees (30 = the whole 2x top_k pool).
    memory_ask_deep_evidence_pool: int = Field(default=30, ge=1)
    # Who picks the memories to open: "llm" = one selection call over the pool; "top" = the
    # top-ranked memories, no LLM call. A request's deep_select overrides.
    memory_ask_deep_evidence_select: Annotated[Literal["llm", "top"], CaseFolded] = "llm"
    # At most this many memories are opened per ask.
    memory_ask_deep_evidence_max_memories: int = Field(default=5, ge=1)
    # Source text per opened memory and in total, so a window-linked
    # memory cannot expand into its whole conversation.
    memory_ask_deep_evidence_max_chars_per_memory: int = Field(default=2500, ge=1)
    # Total source-text characters deep evidence may open across all memories of one ask.
    memory_ask_deep_evidence_max_chars: int = Field(default=8000, ge=1)

    # Default answer length in characters for asks that pass max_answer_chars=0, except
    # breadth questions and structured count/preference/affect answers. 0 = unbounded
    # (synthesis is 88% of ask median latency).
    memory_ask_default_answer_chars: int = 0
    # Selector the temporal trigger uses: "llm" (one selection call, about +3 s p50 on
    # temporal asks only) or "top" (no model call). Per-request deep_select overrides.
    memory_ask_temporal_deep_select: Literal["llm", "top"] = "llm"
    # Client for the deep selector: "cheap" = the cascade's cheap client when configured,
    # else the routed client; "routed" = the client that synthesizes the answer.
    memory_ask_deep_select_tier: Literal["cheap", "routed"] = "cheap"

    # When a query names an identifier (PR/issue number, @handle) that recall missed, inject
    # the memories citing it (exact substring) into the synthesis pool. The lexical leg
    # indexes only alpha tokens >= 3 chars, so bare numbers are otherwise unretrievable.
    # Additive.
    memory_ask_identifier_injection: bool = True
    # After injection, follow identifier-shaped tokens the injected memories name (a value
    # that is a key, a PR naming a commit) for up to this many more exact-substring reads,
    # adding at most memory_ask_identifier_chase_cap rows. 0 = off. Runs only when the
    # question named an identifier.
    memory_ask_identifier_chase_hops: int = 3
    # Identifier injection also reads up to this many verbatim chunks (loom.memory_chunks),
    # ranked by cosine to the question; 0 = facts only.
    memory_ask_identifier_inject_chunks: int = 0
    # A set-shaped question about an identifier reads this many fact rows (and twice as many
    # chunks) from the exact fetch instead of eight.
    memory_ask_identifier_inject_set_facts: int = 24
    # For a bug / error / flaky question, read this many verbatim chunks that cite the
    # identifier and carry fix-or-cause vocabulary (root cause, Closes #, backtrace ...)
    # into the pool ahead of the window. 0 = off.
    memory_ask_decisive_chunks: int = 6
    # For a disposition / premise / classification question, read this many newest facts and
    # chunks citing the identifier into the pool, and tell the synthesizer the later-dated
    # memory is current when memories on one subject disagree. 0 = off.
    memory_ask_identifier_inject_recent: int = 4
    # A question about what was mentioned at a time reads up to this many verbatim chunks
    # whose session date falls in the resolved window (±1 day), cosine-ranked; facts carry
    # the event date, not the mention date. 0 = off.
    memory_ask_mention_window_chunks: int = 4
    # Fact rows the identifier chase (identifiers named by the rows the question's
    # identifier reached) may add across all its hops.
    memory_ask_identifier_chase_cap: int = 8
    # Verbatim chunks the chase may add, counted apart from memory_ask_identifier_chase_cap.
    # 0 = facts only.
    memory_ask_identifier_chase_chunk_cap: int = 8

    # Facet completion: a count question pulls every memory in scope whose key shares the hit
    # keys' attribute facet (user_maya_age -> _age), widening the window to
    # memory_ask_aggregational_window. At most this many rows.
    memory_ask_facet_completion_max: int = 60
    # Bridge completion: the other attributes of the entities the top hits name (key-prefix
    # siblings) join the pool, so a question that names an entity by one attribute can
    # answer with another. The top hits it reads, the rows it adds, the rows per prefix.
    memory_ask_bridge_completion_top_hits: int = 12
    # Most rows bridge completion adds to the pool.
    memory_ask_bridge_completion_max: int = 48
    # Most rows bridge completion adds per entity key prefix.
    memory_ask_bridge_completion_per_prefix: int = Field(default=6, ge=1)
    # Hops over the rows — the
    # proper-noun objects of the top hits and their bridged siblings bridge
    # again, twice at most, so a multi-hop question is answered from rows when
    # the triple plane has no path.
    memory_ask_bridge_row_hops: int = 2
    # Count questions: the raw turns naming the question's literal (a place, a number) join
    # the pool, at most this many — a fact the extractor dropped is still a turn.
    memory_ask_literal_completion_max: int = 40
    # The synthesis window a count question widens to when facet or literal completion
    # adds rows (never narrower than the window it had).
    memory_ask_aggregational_window: int = 40
    # Question decomposition (request `decompose`, off when unset): most
    # sub-questions a decomposed question splits into.
    memory_ask_decompose_max_parts: int = 4
    # Rows retrieved for each decomposed sub-question.
    memory_ask_decompose_part_top_k: int = 8
    # memory.ask general-knowledge bridging: forbid (default)
    # keeps the synthesizer memory-only; allow lets it combine a memory fact
    # with widely known public facts, marking each bridged sentence, and the
    # output's `grounding` says whether it did. Per-request `world_knowledge`
    # overrides. Agents that must never answer beyond the store keep forbid.
    memory_ask_world_knowledge: Annotated[Literal["forbid", "allow"], CaseFolded] = "forbid"
    # The fewest synthesis rows the similarity knee below may leave.
    memory_ask_relevance_gate_min_keep: int = 3
    # Synthesis window for count-shaped questions ("how many X", op=count), whose answer set
    # is scattered across sessions (a missed item sat at rank 18). 0 = no widening.
    memory_ask_count_window: int = 30

    # Kill-switch for code-plane enrichment: when a query names code-shaped identifiers (a
    # symbol, a File.cpp, a snake_case setting), fuse the bound code index's file:line,
    # symbol and recent PRs into the grounding pool. Additive and fail-open. The opt-in is
    # the code namespace binding (_resolve_code_target): with no per-request override and no
    # memory_ask_code_namespace, enrichment is skipped, hence default on.
    memory_ask_code_enrichment: bool = True
    # Symbols the retrieved window names (not only the question) are looked up in the bound
    # code index too, up to this many, one code.symbol definition each, appended after the
    # question's own grounding rows. 0 = off.
    memory_ask_code_hop_symbols: int = 4

    # For each PR the query or grounding references, inject its authoritative
    # code_pr_history record (author, merged_at, title) as a high-trust grounding fact; the
    # synthesizer prefers it over a memory's older claim (_PR_RECORD_NOTE). Gated by the
    # code binding (_resolve_code_target); additive, fail-open, as-of aware. See
    # _inject_pr_facts.
    # A judged arm shows fewer control regressions with it on than off.
    memory_ask_pr_grounding: bool = True

    # For an as_of query, drop grounding memories that cite a PR opened after the anchor (an
    # under-dated bulk ingest can name a later PR). Open dates come from
    # code_pr_history.opened_at; a no-op without as_of or a bound code index; a PR with an
    # unknown open date is never gated. See _gate_future_prs.
    memory_ask_pr_temporal_gate: bool = True

    # Check the code locations a retrieved memory asserts against the live index and flag
    # rotted ones (symbol moved or renamed) so synthesis prefers the current site.
    # Read-time, additive, gated by the code binding; ambiguous symbols are skipped
    # (code_anchor), never false-flagged.
    memory_ask_code_staleness: bool = True

    # Code namespace enrichment reads when the request sets none (a per-request override
    # wins). '' = enrichment skipped: with many repos and branches indexed as separate
    # namespaces there is no safe global default. Pair with memory_ask_code_org when the org
    # differs.
    memory_ask_code_namespace: str = ""

    # An ordering question with a time cue of up to this many days reads the user's turns
    # from that window with the first synthesis, so a member the extractor left as an aside
    # is listed.
    memory_ask_order_window_max_days: int = 95
    # How many user turns dated inside the question's short time window ("a couple of days
    # ago") the not-recorded second reading adds, whatever words they carry.
    memory_ask_cue_window_turns_top_k: int = Field(default=24, ge=1)
    # Dated raw turns the not-recorded second reading recalls.
    memory_ask_when_evidence_top_k: int = Field(default=6, ge=1)
    # Org of memory_ask_code_namespace; "" = code_org, then the ask's own org.
    memory_ask_code_org: str = ""

    # After synthesis one judge call (the cheap client when the cascade is on, else the
    # synth client) labels the answer entailed / neutral / contradicted against the memories
    # it cites (consistency, consistency_score, consistency_note). Off: one extra LLM call
    # per synthesized ask.
    memory_ask_consistency_check: bool = False
    # Where the judge runs. "inline" awaits it and returns the verdict on the wire.
    # "deferred" answers first, leaves the wire fields empty, and writes the verdict as a
    # memory.ask.consistency ledger row (kind='verdict', joined by request_id) plus the
    # contradicted usage event; a precedent is stored only once the verdict
    # is in. A request's consistency field pins either; benches that read the label pin
    # "inline".
    memory_ask_consistency_mode: Literal["inline", "deferred"] = "deferred"
    # When the window holds a pinned or human-verified memory, one judge call asks whether
    # the question presupposes something it contradicts; if so the synthesizer holds the
    # trusted fact and corrects the premise (premise_conflict / premise_note on the wire).
    # One read per ask; an LLM call only with trusted rows in the window.
    memory_ask_premise_check: bool = False
    # Synthesis window for a set question ("what books has she read"), like a count's, since
    # its items sit one per session. 0 = off. MemoryAskInput.set_window overrides.
    memory_ask_set_window: int = 40
    # A count or set question reads up to this many rows (window and search pool) instead of
    # the 40-row pool; 0 = off. Such an ask costs about four times a plain one.
    memory_ask_set_full_scan: int = 300
    # When the whole-set scan widens the pool, cap its chunk share so it is mostly fact
    # rows. 0 = uncapped.
    memory_ask_set_full_scan_chunk_share: float = 0.15
    # A chain question ("the citizenship of the CEO of X") follows the first hop's answer
    # entity into one more retrieval per bridge entity: at most this many entities, this many
    # rows each.
    memory_ask_entity_follow_max: int = 3
    # Rows retrieved for each followed bridge entity.
    memory_ask_entity_follow_top_k: int = 6
    # Passages the knowledge.search leg appends to the synthesis window.
    memory_ask_knowledge_top_k: int = Field(default=5, ge=1)

    # memory.ask keeps declared-external, unattested rows out of the synthesis
    # pool (they still surface as hits). Off = they are synthesized like any other row.
    memory_ask_exclude_unverified_external: bool = True

    # Serve the earlier synthesized answer to the same question and shape, with no LLM call,
    # when the synthesis window is byte-identical (same keys, same content_sha256 per key);
    # otherwise synthesize and store. A per-request precedents field overrides. Max age 0 =
    # only the evidence decides.
    memory_ask_precedents: bool = False
    # Hours a stored precedent stays servable; 0 = no age bound, the evidence alone decides.
    memory_ask_precedents_max_age_hours: int = 0
    # Comma-separated fnmatch globs the precedent flag applies to; '' = every namespace.
    # Scope it to namespaces whose traffic is real repeats (e.g. "support-*"): on a shared
    # server a paired A/B would answer its second arm from the first.
    memory_ask_precedents_namespaces: str = ""
    # With precedents on, an eligible precedent is served before retrieval when no plane its
    # namespaces read was written since (loom.namespace_write_marks), the blacklist is
    # unchanged and every cited row is live with the same hash. Hours a precedent stays
    # servable on that fast path (a bound on rank drift, not staleness); 0 = no bound.
    memory_ask_precedents_fast_max_age_hours: int = 1

    # Exact-prompt LLM response cache for every memory.ask synthesis-path call, keyed by
    # sha256(org, system, user, max_tokens, response_format) per model in
    # loom.llm_response_cache; a miss writes fire-and-forget. The evidence is in the key, so
    # a changed memory misses. The namespace globs (comma-separated fnmatch, '' = all) scope
    # the default; a per-request llm_cache wins (benches measuring synthesis send false).
    memory_ask_llm_cache_default: bool = True
    # Comma-separated fnmatch namespace globs the LLM cache default applies to; '' = all.
    memory_ask_llm_cache_namespaces: str = ""

    # memory.ask pending evidence: the turns of in-flight deferred batches that share words
    # with the question join the synthesis window as raw rows (source="pending").
    # Most pending turns read into the synthesis window per ask.
    memory_ask_pending_evidence_turns: int = Field(default=5, ge=1)
    # Newest in-flight deferred batches the pending-evidence read scans.
    memory_ask_pending_evidence_batches: int = Field(default=20, ge=1)

    # A summary-shaped question ("summarize the report") routes to knowledge.synthesize over
    # the documents the knowledge search concentrates on, falling through to the memory
    # path when none concentrates. Documents that route may synthesize over at most.
    memory_ask_synthesize_route_max_docs: int = Field(default=3, ge=1)
