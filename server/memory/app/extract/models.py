"""What an extraction returns: the fact and typed-record shapes, the
result envelope, the error a caller surfaces, and the prompt version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.llm.client import LLMResult

EXTRACTION_PROMPT_VERSION = "extraction-v1"

# Per-fact cap on `derived` operands. Shared with the derived_facts writer
# (memory/app/entity_projections.py) so the extractor never emits more
# than the writer keeps (a mismatch silently drops the extra operands).
MAX_DERIVED_PER_FACT = 12

# One typed-record field value after coercion; None is the "gave up" marker.
RecordValue = str | int | float | bool | None


@dataclass(frozen=True)
class ExtractedFact:
    memory_key: str
    value: str
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    # Existing-memory keys this fact updates or refines (provenance chain).
    linked_keys: list[str] = field(default_factory=list)
    # Zero-based indices into current_messages that support this fact, so
    # each memory links to its specific evidence rows, not all of them.
    source_message_indices: list[int] = field(default_factory=list)
    # "CORRECT" / "UNCERTAIN" / "WRONG" / "" (judge skipped). WRONG is
    # dropped before bulk-write; UNCERTAIN survives, flagged for review.
    judge_verdict: str = ""
    # Typed (subject, verb, object) triples → loom.entity_triples.
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    # Per relation, the verb as the model wrote it (auxiliaries stripped, not
    # stemmed): the readable attribute a relation-canonical key spells, so a key
    # built from the relation matches the one a model writes for the same fact.
    relation_phrases: list[str] = field(default_factory=list)
    # The key came from a single-valued relation (`relation_canonical_key`): two
    # facts of one batch with such a key are two statements of one attribute.
    key_from_relation: bool = False
    # Structured per-occurrence operands (count/sum/date-diff) → loom.derived_facts.
    # Each item: {predicate, object, category, numeric_value, unit, occurred_at}.
    derived: list[dict[str, Any]] = field(default_factory=list)
    # ISO date the fact is *about*; resolved against observation_date for
    # relative phrases. None when no date is implied.
    temporal_anchor: str | None = None
    # How precisely the source pinned that date, 0..1 per the prompt rubric
    # (memory/domain/temporal_confidence.py); 0 = unscored / no anchor.
    temporal_confidence: float = 0.0
    # ISO date the fact BECAME TRUE in the world (distinct from
    # temporal_anchor); past-tense state changes only. Drives the as-of read.
    valid_at: str | None = None
    # Verbatim quote from a current turn supporting the fact (optional; the
    # prompt does not ask for one) and the server's
    # grade of how the fact is tied to its turns — memory/domain/grounding.py.
    evidence: str = ""
    grounding: str = ""
    # ISO date the fact STOPPED being true; NULL = still valid. Scopes
    # graph traversal so "as of" reads don't surface expired relationships.
    valid_to: str | None = None
    # Foresight window: {"until": date|null, "duration_days":
    # int|null} for a forward-looking or temporary state; None otherwise.
    foresight: dict[str, Any] | None = None
    # Whose fact this is: "user" (default) or "other" — a
    # friend's preference, a colleague's schedule. Rides the row as the tag
    # `about:other`; the profile fold and the reader read it.
    about: str = "user"
    # The user asked the assistant not to remember / to forget this.
    # The entry is NOT written; the memories it names in
    # `linked_keys` (and any same-key fact in the batch) are retired.
    forget: bool = False
    # Deterministic (LLM-free) code facets from extract_code_facets — never
    # coarsen the atomic value. `files` = file paths, `concepts` = symbols/
    # errors/libraries. Both default empty; a non-coding fact carries neither.
    files: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    # LLM-emitted concept phrases the fact is ABOUT ("retry storms after
    # deploys") — ideas, not named things. They become nodes of the
    # co-occurrence graph and `about` triples so two memories
    # about one idea with different entities still connect. Never persisted
    # as a memory column; graph-only.
    topics: list[str] = field(default_factory=list)
    # Short noun-phrase topic of the question an assistant-DIRECT-ANSWER fact
    # addresses. A later follow-up ("what did you say the X was?") has low
    # lexical overlap with the answer; this is NOT persisted as a column and
    # NEVER coarsens the value — under `memory_embed_answer_topic` it is
    # prepended to the EMBEDDED text only. Empty for non-answer facts.
    question_topic: str = ""


@dataclass(frozen=True)
class ExtractedRecord:
    """One typed record extracted under a caller-supplied schema. The
    `record` dict is guaranteed to contain every field declared in the
    schema; values have passed type coercion EXCEPT the fields listed in
    `stubbed_fields`, which are None — the extractor gave up on them after
    its retries. Stored as JSON in `loom.memory.value`."""

    record: dict[str, RecordValue]
    entities: list[str] = field(default_factory=list)
    # (subject, verb, object) triples so typed records join graph search.
    # Empty for schemas with <2 fields.
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    # Fields that could not be validated after _FIELD_RETRY_K attempts and
    # were written as None (never a fabricated value). Lets a caller tell
    # "extracted" from "gave up" — the record dict alone cannot.
    stubbed_fields: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractionResult:
    facts: list[ExtractedFact]
    llm_result: LLMResult
    prompt_version: str = EXTRACTION_PROMPT_VERSION
    # Facts the write-time judge graded WRONG and dropped. Counted (and
    # logged with the key) so a discarded extraction is never invisible.
    dropped_judge_wrong: int = 0
    # Fact objects in the LLM payload discarded by the parser (not a dict,
    # or missing key/value). Same reason.
    dropped_parse: int = 0
    # Facts whose typed relations came from the backfill call, not the first
    # pass. 0 when nothing was missing or the call
    # gave nothing usable.
    relations_backfilled: int = 0
    # Facts the coverage sweep added: a second reading of the
    # turns the first pass cited nowhere. 0 when every turn was cited, the
    # setting is off, or the call gave nothing usable.
    coverage_swept: int = 0
    # How the call met its output budget: "" (fit), "retried" (the first
    # response was cut off, the 2x-budget retry parsed whole) or "salvaged"
    # (the retry was cut off too; complete fact objects were recovered, the
    # tail is lost). Counted in loom.memory_drops as stage extract.truncate
    # so "how often does a session not fit one response" is a number the
    # output-capacity router can be sized on.
    truncation: str = ""
    # The exact-prompt cache key of the first extraction call
    # (sha256 over system, user, max_tokens, response format — the same key
    # `CachedLLMClient` reads by), and the wrapper's hit / miss counts over
    # every call of this extraction (0 / 0 when the cache was off). The key
    # rides the ledger's `args` so the repeat share is readable offline
    # without storing any prompt text.
    prompt_key: str = ""
    llm_cache_hits: int = 0
    llm_cache_misses: int = 0


class ExtractionError(Exception):
    """Raised when the LLM response can't be parsed as a fact list. Caller
    should surface this as a 502 — the operator's LLM is misbehaving and
    silently dropping the user's transcript is the wrong default."""
