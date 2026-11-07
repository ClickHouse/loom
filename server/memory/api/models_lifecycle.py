"""memory.* lifecycle / promotion ops — promote, demote, reject, cross_promote, stack.

(memory.pin shares MemorySetInput/Output with memory.set and lives in memory_crud.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.memory.api.models_common import _Identity

# ============================================================
# memory.promote / memory.demote — shared memory plane
# ============================================================


class MemoryPromoteInput(BaseModel):
    """Promote a single source memory to the namespace's shared layer.

    The explicit, caller-driven path.
    """

    org: str
    namespace: str
    agent: str
    user_id: str = Field(default="-")
    session_id: str = Field(default="-")
    memory_key: str  # source key in loom.memory
    target_key: str | None = None  # default = memory_key
    # Why it was promoted; stored on the shared row and returned by memory.lineage.
    rationale: str = ""
    # Stored as the shared row's `promoted_by`; None records `explicit`.
    approver: str | None = None
    # Days (0–90) the promotion counts as reversible; memory.demote reports it.
    reversible_for_days: int = Field(default=7, ge=0, le=90)
    # On learnings.review-driven promotions, written to
    # loom.shared.promoted_from_candidate_id so memory.lineage resolves via an
    # FK-style lookup. NULL for auto-promote / direct callers (time-window fallback).
    promoted_from_candidate_id: str | None = None


class MemoryPromoteOutput(BaseModel):
    # The key the shared row was written under (`target_key`, else the source key).
    shared_memory_key: str
    promotion_event: str  # UUID, also the audit_log request_id
    # End of the reversal window: now plus `reversible_for_days`.
    reversible_until: datetime


class MemoryDemoteInput(BaseModel):
    org: str
    namespace: str
    # The shared memory to tombstone.
    memory_key: str
    # Why it was demoted; stored on the tombstone row (empty keeps the promotion's rationale).
    rationale: str = ""


class MemoryDemoteOutput(BaseModel):
    # False when the key has no live shared row; a demote outside the window still succeeds.
    demoted: bool
    was_reversible: bool  # whether within the reversal window
    # When the shared row and its embedding were tombstoned; None when nothing was demoted.
    tombstoned_at: datetime | None = None


# ============================================================
# memory.reject — explicit lifecycle state
# ============================================================


class MemoryRejectInput(_Identity):
    """Mark a memory as explicitly rejected (extractor / user review decided the
    candidate fact is wrong), so it isn't re-extracted."""

    # The memory to mark `rejected`, matched on org, namespace, agent, user and key.
    memory_key: str
    # Stored as the row's `rejection_reason`; empty stores none.
    rejection_reason: str = ""


class MemoryRejectOutput(BaseModel):
    # Always True, also when no live row matched (nothing is written then); a locked
    # row in any session refuses the whole reject with 423 LOCKED and writes nothing.
    rejected: bool
    # The server time of the reject.
    rejected_at: datetime | None = None


# ============================================================
# memory.cross_promote — copy one row to a peer namespace
# ============================================================


class MemoryCrossPromoteInput(BaseModel):
    """Cross-namespace promotion with provenance.

    Crosses the namespace boundary entirely, unlike memory.promote (private →
    shared within one namespace). Caller must hold ADMIN on both source and
    target. The target row stores cross_promoted_from_memory_id +
    cross_promoted_from_namespace for lineage. Embeddings aren't copied — the
    target's embedding_worker regenerates them under the target's embedder.
    """

    from_org: str = Field(..., description="Source namespace's org.")
    # The source row's namespace; the caller needs ADMIN on it, and the row must live there.
    from_namespace: str
    from_memory_id: str = Field(..., description="UUID of the source row.")
    # The namespace the copy is written into, in the same org; the caller needs ADMIN on it.
    to_namespace: str
    target_key: str | None = Field(
        default=None,
        description="Override the destination memory_key. Defaults to the source row's memory_key.",
    )
    target_agent: str = Field(
        default="-",
        description="Agent identity to attribute the target row to. Defaults "
        "to '-' so the row reads as namespace-owned, not bound to "
        "any single agent in the target.",
    )
    rationale: str = Field(
        default="",
        description="Human-readable reason; the audit row keeps only its SHA-256 and length.",
    )


class MemoryCrossPromoteOutput(BaseModel):
    promoted: bool  # False = source row not found / tombstoned
    target_memory_id: str = ""  # new UUID in the target namespace
    # The namespace the copy was written into, echoed.
    target_namespace: str
    # The copy's memory_key: `target_key`, else the source row's key.
    target_key: str
    # The source row's id, also stored on the copy as its lineage.
    cross_promoted_from_memory_id: str = ""


# ============================================================
# memory.copy_to — self-service copy into another namespace
# ============================================================


class MemoryCopyToInput(BaseModel):
    """Self-service copy of a memory into another namespace.

    The user-facing counterpart to memory.cross_promote (which is operator-only:
    ADMIN on both ends, addressed by UUID). Here ANY user may copy a memory they
    can READ into any namespace they can WRITE — no ADMIN — and it is addressed
    by (namespace, memory_key), the way the dashboard holds a memory. Non-destructive:
    the source stays; the target records cross_promoted_from_* lineage and is
    re-embedded by the target namespace's own embedding_worker.
    """

    from_org: str = Field(..., description="Org (source and target share it).")
    from_namespace: str = Field(..., description="Source namespace.")
    memory_key: str = Field(..., description="Source memory_key to copy.")
    from_agent: str = Field(
        default="*",
        description="Which agent's row to copy; '*' resolves to the "
        "most-recently-written row for the key (namespace-wide).",
    )
    # The source row's user id, part of the identity the key is looked up under.
    from_user_id: str = Field(default="-")
    # The source row's session id, part of the identity the key is looked up under.
    from_session_id: str = Field(default="-")
    to_namespace: str = Field(..., description="Target namespace. Caller must hold WRITER on it.")
    target_key: str | None = Field(
        default=None,
        description="Destination memory_key. Defaults to the source key.",
    )
    target_agent: str = Field(
        default="-",
        description="Attribution in the target; '-' reads as namespace-owned.",
    )
    rationale: str = Field(
        default="",
        description="Human-readable reason; the audit row keeps only its SHA-256 and length.",
    )


class MemoryCopyToOutput(BaseModel):
    copied: bool  # False = source row not found / tombstoned
    target_memory_id: str = ""  # new UUID in the target namespace
    # The namespace the copy was written into, echoed.
    target_namespace: str
    # The copy's memory_key: `target_key`, else the source row's key.
    target_key: str


# ============================================================
# memory.provenance — where did this memory come from?
# ============================================================


class MemoryEvidenceInput(_Identity):
    """List the source turns a memory was extracted from, with the supporting
    span and whether each link was cited by the extractor, matched lexically,
    or fell back to the whole window."""

    memory_key: str = Field(..., description="The memory whose evidence to list.")
    # At most this many links (1–200), oldest first; `truncated` says more exist.
    limit: int = Field(default=50, ge=1, le=200)


class MemoryEvidenceItem(BaseModel):
    evidence_id: str
    # How the link was made: `extracted_from` = the extractor
    # cited the turn; `lexical_support` = the server matched the fact to its
    # best turn; `window` = nothing matched and the whole window was linked.
    support_type: str
    # cited | lexical | window — the scope the support_type implies. Rows
    # that carry `extracted_from` for a whole-window link with zero-length
    # spans are reported as `window` so an
    # integrity read does not mistake them for citations.
    # `derived`: the link is consolidation lineage and the item's content is
    # the source MEMORY's text, prefixed with its key.
    scope: Literal["cited", "lexical", "window", "derived", "other"]
    role: str = ""
    observed_at: datetime | None = None
    # The evidence turn, clipped to 600 characters.
    content: str = ""
    char_start: int = 0
    char_end: int = 0
    # The supporting substring when the link carries a span.
    span: str = ""


class MemoryEvidenceOutput(BaseModel):
    # False when no live row has the key under the caller's identity (or a wildcard lookup).
    found: bool
    # The requested key, echoed; empty when not found.
    memory_key: str = ""
    # The resolved memory row's id.
    memory_id: str = ""
    # The memory's value as text, clipped to 2000 characters.
    value: str = ""
    # One item per live evidence link on this page, ordered by `observed_at`.
    items: list[MemoryEvidenceItem] = Field(default_factory=list)
    # True when the memory has more live links than `limit`.
    truncated: bool = False
    # Counts by scope over ALL links (not just the returned page).
    cited: int = 0
    # Links scoped `lexical`, over all links.
    lexical: int = 0
    # Links scoped `window`, over all links.
    window: int = 0
    # Links scoped `derived` (consolidation lineage), over all links.
    derived: int = 0


class MemoryProvenanceInput(_Identity):
    """Resolve a memory's origin for user-facing provenance."""

    memory_key: str = Field(..., description="The memory to trace.")


class MemoryProvenanceLink(BaseModel):
    """An authored edge (loom.operator_edges) touching this memory."""

    direction: str  # "out" = this → other; "in" = other → this
    memory_key: str  # the other endpoint
    relation: str = ""
    label: str = ""
    # The edge's justification as written; '' / [] / 1.0 when the author gave
    # none.
    explanation: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    author: str = ""
    run_id: str = ""


class MemoryProvenanceOutput(BaseModel):
    # False when no live row has the key under the caller's identity; the rest is then empty.
    found: bool
    # The row's raw `source_trace` as written; empty for a memory added manually.
    source_trace: str = ""
    # conversation | extracted | observed | consolidated | app | manual | other
    # (derived at read time from the source_trace convention)
    source_kind: str = ""
    # Stored at write time: who wrote the live version and
    # asserted | extracted | observed | derived | imported | external.
    actor: str = ""
    # The row's stamped provenance class (`asserted`, `extracted`, …); see `actor` above.
    source_class: str = ""
    source_label: str = ""  # human-readable origin
    # The conversation thread id, set only when `source_trace` is `conversation:<id>`.
    thread_id: str = ""
    # Whether the source conversation transcript is still retained (recording
    # may be off per-namespace, or the 180-day conversation TTL may have lapsed).
    thread_exists: bool = False
    # The retained thread's title; empty when the thread is gone or untitled.
    thread_title: str = ""
    agent: str = ""
    session_id: str = ""
    run_id: str = ""
    # The live version's `created_at`, as a string.
    written_at: str = ""
    # Caller-supplied pointer to the primary cited source (file/doc/URL).
    source_resource_id: str = ""
    # Authored "derived_from"/etc. edges (memory.link) touching this memory.
    links: list[MemoryProvenanceLink] = Field(default_factory=list)


# ============================================================
# memory.stack — token-budgeted wake-up bundle
# ============================================================


class MemoryStackInput(_Identity):
    """Compose a token-budgeted recall bundle for agent wake-up.

    Returns profile (profile.render), top pinned memories, recent-recall
    memories, optional matched procedures (procedure.match when seed_query is
    set), and optional knowledge top-K, all trimmed to `token_budget`. Pure
    composition over existing planes — no LLM, no new SQL — so a caller building
    a system prompt makes one call instead of 4-5 plus budget arithmetic.
    """

    # Estimated tokens (chars / 4) all sections share; an entry that would
    # overflow it ends its section.
    token_budget: int = Field(default=4000, ge=200, le=32000)
    # At most this many pinned memories, newest version first (0 skips the section).
    pinned_limit: int = Field(default=10, ge=0, le=100)
    # At most this many recalled, unpinned memories, most recalled first (0 skips the section).
    recent_limit: int = Field(default=20, ge=0, le=200)
    # True adds a knowledge section searched on `seed_query` (or the recent-recall excerpt).
    include_knowledge: bool = False
    # Knowledge hits to request when `include_knowledge` is set; 0 skips the section.
    knowledge_top_k: int = Field(default=3, ge=0, le=20)
    # Drives the procedure.match query and the knowledge-search query. Without it
    # the procedure section is skipped and knowledge falls back to the recent-recall excerpt.
    seed_query: str = ""
    # Procedures to match on `seed_query`; 0, or no `seed_query`, skips the section.
    procedure_limit: int = Field(default=3, ge=0, le=20)


class _StackEntry(BaseModel):
    section: Literal["profile", "pinned", "recent_recall", "procedure", "knowledge"]
    memory_key: str = ""
    memory_type: str = ""
    value: str = ""
    tokens: int  # rough estimate: len(value) // 4
    pinned: bool = False
    recall_count: int = 0
    last_recalled_at: datetime | None = None
    # The memory's writer (`human:<subject>` / `process:<subject>`); "" for the profile
    # and knowledge entries and for rows with no recorded actor.
    actor: str = ""
    # The namespace the row lives in, when it is not the request's: a procedure matched
    # in another of the caller's namespaces. "" = the request's.
    namespace: str = ""


class MemoryStackOutput(BaseModel):
    # Profile, pinned, recent_recall, procedure, knowledge entries, in that section order.
    entries: list[_StackEntry]
    # Estimated tokens the returned entries use (chars / 4 each).
    tokens_used: int
    # The `token_budget` the bundle was trimmed to, echoed.
    tokens_budget: int
    truncated: bool  # true when at least one section was trimmed to fit
    section_counts: dict[str, int]  # how many entries per section
