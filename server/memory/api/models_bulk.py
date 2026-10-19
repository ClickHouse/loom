"""memory.* batch / extraction ops — bulk_set, set_from_messages, bulk_get, import_curated."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from server.memory.api.models_common import MemorySetPeer, MemoryType, _Identity
from server.memory.api.models_crud import MemoryGetOutput
from server.operations.base import MCP_HIDDEN

# ============================================================
# memory.bulk_set / memory.bulk_get
# ============================================================


class _BulkSetEntry(BaseModel):
    memory_key: str
    memory_type: MemoryType = "semantic"
    value: dict[str, Any] | list[Any] | str | int | float | bool
    confidence: float = 1.0
    source_trace: str = ""
    # `external` / `imported` only — a downgrade the caller may declare; the
    # server decides the rest.
    source_class: str | None = None
    ttl_seconds: int | None = None
    tags: list[str] = Field(default_factory=list)
    # Extracted named things for memory.related + search entity_filter. Normally
    # populated by the entity worker; set_from_messages gets them from extraction.
    entities: list[str] = Field(default_factory=list)
    # Typed (subject, relation, object) triples → loom.entity_triples; boost
    # memories whose stored triples match query-extracted triples.
    relations: list[tuple[str, str, str]] = Field(default_factory=list)
    # Per-occurrence operands (count/sum/date-diff) for multi-hop derived answers
    # → loom.derived_facts. Each: {predicate, object, category, numeric_value, unit, occurred_at}.
    derived: list[dict[str, Any]] = Field(default_factory=list)
    # ISO date the fact is ABOUT (distinct from created_at); read by date-overlap
    # rank fusion in graph mode. None = no anchor.
    temporal_anchor: str | None = None
    # How precisely the source pinned temporal_anchor (0..1);
    # 0 = unscored. Scales the date-match boost; unscored keeps full weight.
    temporal_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # Real-world time the fact became true (distinct from temporal_anchor = the
    # date it's about). None = unknown; as-of reads fall through to created_at.
    valid_at: datetime | None = None
    # Real-world time the fact STOPPED being true; flows into entity_edges/triples
    # valid_to so the as-of graph filter excludes facts that ended before the query
    # date. NULL = still valid (common case).
    valid_to: datetime | None = None
    # Char cap on stringified value — same as MemorySetInput.char_limit. Required
    # on bulk-seeding entries or the cap is silently dropped.
    char_limit: int | None = None
    pinned: bool = False  # explicit-save flag; same as MemorySetInput.pinned
    # Code facets: file paths + code concepts (functions/symbols/libraries/error
    # tokens) from the extraction path, persisted as array filters for
    # "what touches this file" / "what fixed this error".
    files: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    # Concept phrases the fact is ABOUT (extractor `topics`).
    # Graph-only: they become co-occurrence nodes and `about` triples, not a
    # memory column.
    topics: list[str] = Field(default_factory=list, max_length=3)
    # Answer-topic for genuine assistant-answer facts only: the short noun-phrase
    # topic of the answered question. NOT a memory column — when
    # memory_embed_answer_topic is on it is prepended to the EMBEDDED text only,
    # so a topic-only follow-up retrieves the answer while the value stays intact.
    question_topic: str = ""
    # Grounding grade + verbatim quote from the extraction pass; "" when the writer did
    # not grade (memory.set, imports).
    evidence: str = ""
    grounding: str = ""
    # Sensitivity tier; '' = detect from the value.
    sensitivity: Literal["", "low", "medium", "high"] = ""


class MemoryBulkSetInput(_Identity):
    """Insert many memory entries in one batched CH insert.

    All entries share the caller's (org, namespace, agent, user_id,
    session_id) identity tuple. Per-entry overrides go in `_BulkSetEntry`.
    Embeddings for searchable types are computed in the same batch.
    """

    # The entries to write, each with its own key, value and per-entry fields; all
    # share the caller's identity.
    entries: list[_BulkSetEntry] = Field(..., min_length=1, max_length=1000)
    # Treat this call's entries as one document and prepend a
    # model-written one-line context to what each entry EMBEDS and to its
    # content_excerpt (value untouched). One model call per entry; gated by
    # LOOM_MEMORY_BULK_CONTEXTUALIZE_ALLOWED. Off by default.
    contextualize: bool = False
    # See MemorySetFromMessagesInput.detect_updates (same semantics).
    detect_updates: bool | None = None


class _Message(BaseModel):
    role: str = "user"
    content: str
    # When this message was said. Optional; when present the
    # evidence row's observed_at is this moment, the extractor sees it as
    # `[role @ YYYY-MM-DD HH:MM]` and anchors state-at-the-time facts (a
    # feeling, an activity, an observation) to it, so "how did I feel around
    # 08:10" resolves to the turn, not the session. Facts that name another
    # date keep that date. Absent → the session-level date.
    timestamp: datetime | None = None
    # Who said it, when the role alone does not say (two humans in one
    # transcript, a meeting, an imported chat). Rendered to the extractor as
    # `[user Priya]`; a fact cited to one speaker's turns gets a
    # `speaker:<name>` tag and the name as an entity.
    name: str = Field(default="", max_length=80)


class MemorySetFromMessagesInput(_Identity):
    """Run LLM extraction on `messages` and write the extracted facts.

    The right path when the caller can't or shouldn't decide what to save
    inline — raw API consumers, batch importers, attention-saturated callers.
    Pre-decided memories should go through memory.set; running the extractor
    on already-clean data wastes tokens.
    """

    # 400, not 100: an unattended session or a grouped batch of sessions is
    # longer than a chat turn, and topic segmentation exists
    # to split exactly that into topic-sized extraction calls.
    messages: list[_Message] = Field(..., min_length=1, max_length=400)
    # Applied to every extracted fact, merged with the LLM-extracted tags.
    extra_tags: list[str] = Field(default_factory=list)
    # The memory keys the session's Loom tools SERVED (the plugin's hooks
    # stash them). An extracted fact whose
    # key carries a served key's subject words is a recall echo even when the
    # session's text never named the key.
    recalled_keys: list[str] = Field(default_factory=list, max_length=200)
    # The text the session's own Loom tool calls carried —
    # the values they wrote, the queries they asked, the results they got
    # (the plugin reads them from the transcript at SessionEnd). A fact that
    # restates one is an echo of what the tool already persisted or served.
    session_values: list[str] = Field(
        default_factory=list, max_length=100, json_schema_extra=MCP_HIDDEN
    )
    # Ceiling on the extraction call; default gives headroom for ~40+ facts.
    max_tokens: int = Field(default=4096, ge=128, le=8192, json_schema_extra=MCP_HIDDEN)
    # Optional second-pass extractor judge (adds an LLM call); off by default.
    judge_extraction: bool = Field(default=False, json_schema_extra=MCP_HIDDEN)
    # Typed extraction. When set, the extractor produces ONE record matching the
    # schema instead of free-form facts. Each field spec is either a type name
    # ("string"|"number"|"integer"|"boolean") or a list of strings (enum — the
    # LLM must pick one; use for category fields so aggregate doesn't fragment).
    # Validated; retries once with a stricter prompt on failure. Requires
    # `memory_key`; recovered fields land in `value` as a JSON object.
    record_schema: dict[str, str | list[str]] | None = None
    memory_key: str | None = None  # required iff record_schema is set
    # ISO YYYY-MM-DD anchor for relative time references ("yesterday", "last week").
    # Empty = keep relative references verbatim.
    observation_date: str = ""
    # Per-call pin of the extractor's exact-prompt response
    # cache. None = the traffic class's policy (bench on), else off. A bench that
    # measures extraction itself sends false, as `memory.ask`'s A/Bs do.
    llm_cache: bool | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # Value-update supersession (memory.app.value_updates): after the write,
    # link each new memory that restates an existing one with a different
    # value token, marking the older side superseded. None = deployment
    # setting memory_extract_value_updates (default off).
    detect_updates: bool | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # Prior turns from the SAME session not being extracted this call — lets the
    # extractor resolve pronouns/follow-ups/cross-turn references without
    # re-extracting earlier facts. Capped at 20 to bound prompt cost.
    last_messages: list[_Message] = Field(default_factory=list, max_length=20)
    # Per-request extraction-prompt suffix for workload-specific tuning.
    extraction_instructions: str = Field(default="", max_length=4096)
    # The coverage sweep (turn- and sentence-level
    # second reading) per call — None = the namespace, then the density gates. A
    # statement-dense ingest (a fact list, a long update) asks for it; a chat
    # transcript keeps the default, which its asides do not repay.
    coverage_sweep: bool | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # Per-call workflow override. Empty = default pipeline
    # (fetch_existing → llm_extract → hash_dedup → bulk_write). Each step ID names
    # a registered step (loom.fetch_existing_memories / loom.llm_extract /
    # loom.hash_dedup / loom.bulk_write, …); an unknown ID is skipped and reported
    # in pipeline_errors. The server makes no outbound call on a caller's behalf,
    # so an HTTP hook step (`hook.*`) is refused.
    pipeline_steps: list[dict[str, Any]] = Field(
        json_schema_extra=MCP_HIDDEN,
        default_factory=list,
        max_length=16,
    )

    @field_validator("pipeline_steps")
    @classmethod
    def _no_hook_steps(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hooks = [str(s.get("step_id")) for s in v if str(s.get("step_id", "")).startswith("hook.")]
        if hooks:
            raise ValueError(f"HTTP hook steps are not supported: {', '.join(hooks)}")
        return v

    # Fallback valid_at for facts the extractor didn't anchor itself. None = NULL.
    valid_at: datetime | None = None
    # Capture now, extract later. True persists the request to
    # loom.memory_extraction_queue and returns in tens of milliseconds with
    # `deferred=true` + `queue_id`; the extraction_queue worker replays it
    # through this same path. The memories are identical, only later — until
    # then memory.search / memory.ask report `pending_extraction` for the
    # scope. Use on the agent hot path; keep False when the caller needs the
    # written keys back (record_schema flows, tests, backfills that poll).
    defer: bool = False


class MemorySetFromMessagesOutput(BaseModel):
    """One entry per extracted fact that was written. `tokens_in` /
    `tokens_out` are reported back so the caller can attribute LLM cost
    to a save batch."""

    # Key and version of each extracted fact written; empty on a deferred call.
    written: list[_BulkSetResult] = Field(default_factory=list)
    # Prompt tokens of the extraction LLM calls.
    tokens_in: int = 0
    # Completion tokens of the extraction LLM calls.
    tokens_out: int = 0
    # The LLM provider that ran the extraction; '' when deferred.
    llm_provider: str = ""
    # The model that ran the extraction; '' when deferred.
    llm_model: str = ""
    # Extraction calls made: 1, or the number of topic segments.
    segments: int = 1
    # Facts written as time-bounded `foresight` rows.
    foresight: int = 0
    # Memories retired because the user asked to forget them,
    # and their keys.
    forgotten: int = 0
    # The keys of the memories those forget requests retired.
    forgotten_keys: list[str] = Field(default_factory=list)
    # Requests to forget read from the user's own sentences;
    # with `forgotten` it says how many found a row to retire.
    forget_requests: int = 0
    # Rows written as another person's (`about:other`), whether the extractor
    # said so or the sentence's subject did.
    about_other: int = 0
    # The user's facts read from the assistant's premises ("since you enjoy
    # X…"), added to the batch.
    premise_facts: int = 0
    # Which planner cut them: whole | size. With `segments`
    # this makes the segmentation fire rate one GROUP BY on agent_events
    # (out_segmenter / out_segments).
    segmenter: str = "whole"
    # Subset of tokens_in the provider served from its prompt cache (billed
    # at a discount). With segments > 1 this is how much of the repeated
    # static prompt was actually shared.
    cached_tokens_in: int = 0
    # Extracted entity types / relation verbs outside the namespace's ontology
    # (loom.namespace_ontology). Never blocks the write. Empty on cold namespaces.
    ontology_warnings: list[str] = Field(default_factory=list)
    # Evidence-first provenance: set when raw source evidence was persisted before
    # extraction and linked to the written memories.
    evidence_ids: list[str] = Field(default_factory=list)
    # Links written from the new memories to the stored source evidence rows.
    belief_source_links: int = 0
    # Fail-open pipeline steps that logged-and-skipped (operator hook
    # unreachable, fetch_existing failed, unknown step id, ...). Empty on a
    # clean run. Extraction failure itself is fail-closed (502), never here.
    pipeline_errors: list[str] = Field(default_factory=list)
    # Older memories this ingest marked superseded by a value update
    # (memory.app.value_updates; 0 when detection is off).
    value_updates_linked: int = 0
    # Schema mode only: record_schema fields the extractor could not validate
    # after its retries and wrote as null (never a fabricated value). Empty
    # when every field validated or on the messages path.
    stubbed_fields: list[str] = Field(default_factory=list)
    # Schema mode only: what the response skipped and why, e.g. the
    # memory_records projection for a caller bounded to one user. Empty when
    # nothing was skipped.
    degraded: list[str] = Field(default_factory=list)
    # defer=true: nothing extracted yet; `written` is empty and `queue_id` is
    # the handle memory.extraction_status reports on.
    deferred: bool = False
    # The queued batch's id for `memory.extraction_status`; '' when inline.
    queue_id: str = ""
    # "" (inline) | "flag" (the caller passed defer=true) | "allowlist" (the
    # deployment's extraction_defer_agents chose for the caller)
    deferred_by: str = ""
    # Wall time per stage of the ingest: handler stages (evidence_write,
    # pipeline, projections, ontology, total), pipeline steps (`step.<id>`)
    # and bulk-write phases (`bulk.<phase>`).
    stage_ms: dict[str, float] = Field(default_factory=dict)
    # Where the observation date the facts were anchored to came from:
    # "caller" | "request_time" | "" (none — undated rows).
    observation_date_source: str = ""
    # The sha256 keys of the rendered extractor prompts (one
    # per segment; the exact-prompt cache's own key, no text) and the cache's
    # hit / miss counts over this extraction's calls (0 / 0 = cache off).
    extract_prompt_keys: list[str] = Field(default_factory=list)
    # Extraction calls answered from the exact-prompt response cache.
    llm_cache_hits: int = 0
    # Extraction calls the cache could not answer.
    llm_cache_misses: int = 0


class _BulkSetResult(BaseModel):
    memory_key: str
    version: int
    # The per-row verdict memory.set returns, so a caller of
    # bulk_set can act on one entry without decoding the aggregate lists.
    # Empty when the entry was not checked (non-searchable, past the cap).
    conflict_verdict: str = ""
    peers: list[MemorySetPeer] = Field(default_factory=list)
    next: str = ""


class MemoryBulkSetOutput(BaseModel):
    # One result per entry, in order: key, version (0 when not inserted) and verdict.
    written: list[_BulkSetResult]
    # Keys whose live row is pinned: skipped (result version=0) rather than
    # overwritten, since overwriting would also unpin (entries default pinned=False).
    skipped_pinned: list[str] = Field(default_factory=list)
    # Keys whose live row is locked read_only: skipped (result version=0), where memory.set
    # would answer 423 LOCKED. Unlocking is the owner's act (memory.unlock).
    skipped_locked: list[str] = Field(default_factory=list)
    # Conflict verdicts, the same four memory.set produces:
    # canonical restatement of an existing row → not inserted, existing row
    # reinforced; close to a pinned peer → not inserted; same fact in other
    # words → inserted as status=duplicate; close-but-different → inserted and
    # cross-linked as status=conflicted.
    merged: list[str] = Field(default_factory=list)
    # Older rows marked superseded by a value update in this batch.
    value_updates_linked: int = 0
    # Keys not inserted because they are too close to a pinned row.
    rejected_pinned: list[str] = Field(default_factory=list)
    # Keys inserted as `status=duplicate`: the same fact as a live row in other words.
    duplicates: list[str] = Field(default_factory=list)
    # Keys inserted and cross-linked as `status=conflicted` with the rows they
    # contradict.
    conflicted: list[str] = Field(default_factory=list)
    # Entries past bulk_conflict_check_max_entries, written without a check.
    unchecked: int = 0
    # Entries declared procedural whose value is not a procedure body, stored
    # as semantic tagged retyped_from:procedural.
    retyped: list[str] = Field(default_factory=list)
    # Entries not written because a live row in the same identity already
    # carries the same content: entry key -> the keeper's key.
    # The keeper was reinforced instead.
    identity_duplicates: dict[str, str] = Field(default_factory=dict)
    # Wall time per phase of the write (existing_lookup, entity_resolution,
    # prepare, embed, verdicts, insert, value_updates, projections, total) —
    # Diagnostic; readers tolerate unknown keys.
    stage_ms: dict[str, float] = Field(default_factory=dict)


MemorySetFromMessagesOutput.model_rebuild()


class MemoryBulkGetInput(_Identity):
    """Fetch many memory entries by key in one CH round-trip."""

    # Keys to read under this identity; a `chunk:memory.set:<key>:<n>` id from a
    # search reads its parent fact when no row is stored under that key, and the
    # parent answers under the id it was asked by.
    memory_keys: list[str] = Field(..., min_length=1, max_length=1000)
    # Whitelist of output field names applied to every entry
    # in `results` (each keeps `found`). Empty = every field. See
    # MemoryGetInput.fields.
    fields: list[str] = Field(default_factory=list, max_length=64)


class MemoryBulkGetOutput(BaseModel):
    # Named `results` (not `found`) to avoid colliding with the per-row
    # `found: bool` inside MemoryGetOutput. Every value is a hit; `missing` is misses.
    results: dict[str, MemoryGetOutput] = Field(default_factory=dict)
    # Asked keys with no live row under this identity.
    missing: list[str] = Field(default_factory=list)


# memory.import_curated — turn a curated doc (e.g. AGENTS.md / CLAUDE.md) into
# memories. Splits on markdown headings; each section → one memory_key=value,
# written via handle_memory_set so check_on_commit fires (pinned-reject,
# semantic-conflict, canonical-merge), with source_trace=curated_doc://<uri>.
# The import choice is the trust signal — no human review step; write-blocking
# conflicts surface in the response.


class MemoryImportCuratedInput(BaseModel):
    org: str
    namespace: str
    # Raw markdown; caller fetches the doc (the server doesn't read the filesystem).
    content: str = Field(..., min_length=1, max_length=2_000_000)
    # Stored in source_trace for lineage.
    source_uri: str = Field(default="", max_length=512)
    # Heading depth to split on; h2 is the typical AGENTS.md / CLAUDE.md layout.
    heading_level: int = Field(default=2, ge=1, le=6)
    # Applied to every imported memory, on top of the auto source-kind + heading tags.
    extra_tags: list[str] = Field(default_factory=list)


class MemoryImportCuratedRow(BaseModel):
    memory_key: str
    section_heading: str
    bytes_written: int
    verdict: str  # "written" | "merged" | "duplicate" | "rejected_pinned" | "skipped"
    rejection_reason: str = ""
    pinned_blocker_id: str | None = None


class MemoryImportCuratedOutput(BaseModel):
    # One entry per heading section: its key, verdict and bytes written.
    rows: list[MemoryImportCuratedRow]
    # Sections written as memories.
    written: int
    # Sections the write marked as duplicates of an existing memory.
    merged: int
    # Sections refused because their key holds a pinned memory.
    rejected: int
    # Sections not written: an empty body or another write error.
    skipped: int


class MemoryExtractionStatusInput(_Identity):
    """State of deferred set_from_messages batches.

    Scope follows memory.search: agent "*" and the identity sentinel widen the
    view. `queue_id` narrows to one batch; `status` to one lifecycle state.
    """

    agent: str = "*"
    # One batch, by the id a deferred `memory.set_from_messages` returned; '' = every
    # batch in scope.
    queue_id: str = ""
    # Only batches in this state; '' = any.
    status: Literal["", "pending", "running", "done", "failed"] = ""
    # At most this many batches returned.
    limit: int = Field(default=50, ge=1, le=500)


class _ExtractionQueueRow(BaseModel):
    queue_id: str
    agent: str
    user_id: str
    session_id: str
    status: str
    attempts: int
    message_count: int
    content_chars: int
    written: int
    last_error: str
    enqueued_at: datetime | None = None
    updated_at: datetime | None = None


class MemoryExtractionStatusOutput(BaseModel):
    # The queued batches in scope: state, attempts, sizes and last error.
    batches: list[_ExtractionQueueRow] = Field(default_factory=list)
    # Not yet extracted (pending + running) in the requested scope.
    pending: int = 0
    # Of the returned batches, how many ended `failed`.
    failed: int = 0
