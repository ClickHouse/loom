"""memory.* relationship / graph ops.

dedup_list, dedup_resolve, related, lineage, link, assign_category.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.memory.api.models_common import MemoryType, _Identity
from server.operations.base import MCP_HIDDEN


class MemoryLineageInput(_Identity):
    """Assemble the provenance chain for a SHARED memory.

    Walks back-pointers loom.shared → loom.audit_log (on promotion_event UUID)
    → loom.learnings_queue (best-effort match on memory_key + decided_at near
    promoted_at). `scope='shared'` is the only supported value; private memories
    carry their own source_trace and need no walk.
    """

    # The shared memory to trace; a key with no live shared row is read from `loom.memory` tags.
    memory_key: str
    # Only `shared` is accepted.
    scope: Literal["shared"] = "shared"
    # Window (s) each side of promoted_at for the best-effort learnings_queue match.
    candidate_match_window_s: int = Field(default=300, ge=1, le=86400)


class _LineageSupporter(BaseModel):
    """One parsed identity tuple from `loom.shared.promoted_from`.

    Format on the wire: `org/namespace/agent/user_id/session_id/memory_key`.
    """

    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str


class _LineageCandidate(BaseModel):
    candidate_id: str
    source_kind: str
    supporter_count: int
    rationale: str
    decided_at: datetime
    decided_by: str
    match_confidence: Literal["exact", "inferred"]
    # Populated for source_kind='conversation_seeder' candidates so reviewers can
    # walk back to the originating conversation turn. Empty otherwise.
    seeded_from_thread_id: str = ""
    seeded_from_msg_id: str = ""


class _LineageAuditEvent(BaseModel):
    ts: datetime
    event_type: str
    actor: str
    request_id: str


class MemoryLineageOutput(BaseModel):
    # False when the key has no live row in `loom.shared` or `loom.memory`.
    found: bool
    # The requested key, echoed.
    memory_key: str
    # Promotion-event metadata, lifted off the loom.shared row.
    promotion_event: str = ""
    # Who promoted it: `auto` for the auto-promote worker, else the approver;
    # for a memory-only row, the row's `user_id`.
    promoted_by: str = ""
    # When it was promoted; for a memory-only row, the row's `created_at`.
    promoted_at: datetime | None = None
    # The promotion's rationale; for a memory-only import, `Imported via <source kind>`.
    rationale: str = ""
    # Direct source pointers when the row came from a conversation/document/import
    # resource rather than a shared-memory promotion.
    source_trace: str = ""
    # The source resource id stored on the memory row; empty for a shared-row walk.
    source_resource_id: str = ""
    # auto = auto-promote worker; review = learnings.review; unknown = unmatched.
    mechanism: Literal["auto", "review", "import", "unknown"] = "unknown"
    # The identities the shared row was promoted from; empty for a memory-only row.
    supporters: list[_LineageSupporter] = Field(default_factory=list)
    # None for mechanism='auto' (bypasses the queue), GC'd queue rows, or a
    # missed timing window.
    candidate: _LineageCandidate | None = None
    # Up to 100 audit rows carrying the promotion event as request id; empty without one.
    audit_chain: list[_LineageAuditEvent] = Field(default_factory=list)


class MemoryDedupListInput(BaseModel):
    """List clusters of status='duplicate' rows in a namespace.

    Each cluster is one canonical row + the duplicates that point at
    it via `duplicate_of`. ADMIN-only — operator triage surface.
    """

    org: str
    namespace: str
    # At most this many clusters (1–200), largest first; each lists up to 50 duplicates.
    limit: int = Field(default=50, ge=1, le=200)


class _DedupDuplicate(BaseModel):
    memory_id: str
    memory_key: str
    agent: str
    created_at: datetime | None = None


class _DedupCluster(BaseModel):
    canonical_id: str
    canonical_key: str = ""
    canonical_agent: str = ""
    dup_count: int
    duplicates: list[_DedupDuplicate] = Field(default_factory=list)


class MemoryDedupListOutput(BaseModel):
    # Live duplicate rows grouped by their canonical, ordered by duplicate count descending.
    clusters: list[_DedupCluster]


class MemoryDedupResolveInput(BaseModel):
    """Resolve one duplicate pair.

    `canonical_id` + `duplicate_id` are the two memory ids of one
    `memory.dedup_list` cluster entry: the duplicate row must be live in
    this namespace, marked `duplicate` and point at the canonical id.
    `confirm_canonical_key` must equal the canonical row's key —
    mid-weight defense against UI-dropdown accidents (mirrors memory.clear).

    Decisions:
      - keep_canonical: tombstone the duplicate; canonical wins.
      - keep_duplicate: tombstone the canonical; flip the duplicate
        back to status='active' and clear its duplicate_of pointer.
      - keep_both: both stay live; the duplicate flips to 'active' and
        its duplicate_of clears. A later write can flag the pair again.
    """

    org: str
    namespace: str
    # The cluster's `canonical_id` from memory.dedup_list; must be a live row here (else 404).
    canonical_id: str
    # A `duplicates[].memory_id` of that cluster, marked duplicate of `canonical_id` (else 404).
    duplicate_id: str
    # Which side stays live; see the class docstring for each value.
    decision: Literal["keep_canonical", "keep_duplicate", "keep_both"]
    # Must equal the canonical row's key, else 400 CONFIRM_CANONICAL_KEY_MISMATCH.
    confirm_canonical_key: str = Field(..., min_length=1, max_length=256)
    # Removed, as a key does not name one row: accepted, unused and named in `ignored_args`.
    canonical_key: str = Field(default="", json_schema_extra=MCP_HIDDEN)
    # Removed, as a key does not name one row: accepted, unused and named in `ignored_args`.
    duplicate_key: str = Field(default="", json_schema_extra=MCP_HIDDEN)


class MemoryDedupResolveOutput(BaseModel):
    # The canonical row's key.
    canonical_key: str
    # The duplicate row's key.
    duplicate_key: str
    # The decision applied, echoed.
    decision: str
    # True when `keep_duplicate` tombstoned the canonical row.
    canonical_tombstoned: bool = False
    # True when `keep_canonical` tombstoned the duplicate row.
    duplicate_tombstoned: bool = False
    # Request fields the server did not apply: unknown keys, and the removed canonical_key / duplicate_key.
    ignored_args: list[str] = Field(default_factory=list)


class MemoryDedupCollapseInput(BaseModel):
    """Collapse every duplicate pair of a namespace onto its canonical — no
    LLM, every key kept, nothing tombstoned.

    A `duplicate` row already defers to its canonical and the read path hides
    it (memory.search seats the canonical in its slot, memory.list skips it,
    memory.get names it in `canonical_key`). This pass repairs the search
    leg's mirror where it still says `active` for a duplicate, so the leg
    skips the row before the vector sort. `dry_run` (default) only counts:
    `pairs`, `orphans` (canonical no longer active), `mirror_stale`.
    """

    org: str
    namespace: str
    # Only duplicates whose memory_key starts with this ("" = the namespace).
    prefix: str = ""
    # True only counts; False re-emits the stale mirror rows as `duplicate`.
    dry_run: bool = True
    # Pairs examined per call, by key; `truncated` says a second call is due.
    limit: int = Field(default=500, ge=1, le=5000)
    # Pairs echoed back in `sample`.
    sample: int = Field(default=20, ge=0, le=200)


class _DedupCollapsePair(BaseModel):
    duplicate_key: str
    canonical_key: str


class MemoryDedupCollapseOutput(BaseModel):
    namespace: str
    # The key prefix the pass covered, echoed ("" = the whole namespace).
    prefix: str = ""
    # Whether this was a count-only pass, echoed.
    dry_run: bool = True
    # Live `duplicate` rows naming a canonical (bounded by `limit`).
    pairs: int = 0
    # ... whose canonical is a live active row.
    canonical_live: int = 0
    # ... whose canonical is gone: memory.resolve_orphan_duplicates re-activates them.
    orphans: int = 0
    # Mirror rows of live pairs still carrying a status other than `duplicate`.
    mirror_stale: int = 0
    # Mirror rows re-emitted as `duplicate` (0 under dry_run).
    restamped: int = 0
    # True when `limit` pairs were read, so more may remain: call again.
    truncated: bool = False
    # Up to `sample` pairs whose canonical is live, as duplicate → canonical keys.
    sample: list[_DedupCollapsePair] = Field(default_factory=list)


class MemoryRelatedInput(_Identity):
    """Find memories sharing entities with the source memory.

    Entity-overlap recall for the indirect-connection case vector similarity
    misses. Ranked by overlap count (deterministic, symbol-anchored). Requires
    LOOM_ENTITY_EXTRACT_ENABLED=true; with the extractor off the source's
    entities are [] and this returns [] with reason="source_has_no_entities".
    """

    # The source memory, looked up under the caller's full identity tuple.
    memory_key: str
    # At most this many related memories (1–100), most shared entities first.
    top_k: int = Field(default=10, ge=1, le=100)
    # Min overlapping entities to qualify. 1 = any shared entity.
    min_overlap: int = Field(default=1, ge=1, le=32)


class _MemoryRelatedHit(BaseModel):
    memory_key: str
    memory_type: MemoryType
    content_excerpt: str
    overlap_count: int
    shared_entities: list[str]


class MemoryRelatedOutput(BaseModel):
    # Live memories sharing entities with the source (the source excluded), by overlap.
    results: list[_MemoryRelatedHit]
    # The entities on the source row; empty when it is missing or has none.
    source_entities: list[str]
    # Reason code when results=[] for a precondition (not an empty result set):
    # "source_not_found" | "source_has_no_entities".
    reason: str = ""


class MemoryLinkInput(BaseModel):
    """Author an explicit edge between two memories (or two entities).

    Unlike co-occurrence-derived loom.entity_edges, this records a relationship
    the text never makes explicit. Symmetric reads — graph-mode search UNIONs
    operator_edges with entity_edges. Pass tombstone=True to rescind a prior
    link (row kept for audit; reads filter tombstoned_at IS NULL).
    """

    org: str
    namespace: str
    # Typically memory_keys, but bare entity strings work too; graph_mode SQL
    # treats both endpoints uniformly.
    from_key: str
    # The edge's object endpoint (a memory key or an entity string).
    to_key: str
    # Relation verb, canonicalized on write (memory/domain/edge_relations.py):
    # supports, depends_on, caused, contradicts, refines, implements,
    # validates, supersedes, blocks, fixes, derived_from, related_to. Other
    # verbs pass through normalized; an EMPTY relation is stored as related_to
    # and flagged on the output. Direction matters: from_key
    # is the subject ("A supersedes B").
    relation: str = ""
    # Free-text label stored on the edge and returned by memory.provenance.
    label: str = ""
    # Why this edge exists, in one human-readable sentence, and the concrete
    # observations that support it. Similarity is a discovery hint, not a
    # relationship: an edge should be explainable to the reader who finds it
    # in memory.provenance a month later.
    explanation: str = Field(default="", max_length=2000)
    # Up to 10 supporting observations; each is trimmed to 500 chars, blanks dropped.
    evidence: list[str] = Field(default_factory=list, max_length=10)
    # Confidence that the RELATIONSHIP holds — not confidence in either memory.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # The run that asserted the edge (run.begin); joins the edge to its ledger.
    run_id: str = "-"
    weight: float = 1.0  # edge strength; higher = stronger preference at graph-mode rank time
    tombstone: bool = False  # rescind a prior edge (writes a tombstone row)


class MemoryLinkOutput(BaseModel):
    # True when an edge was written; False when the call wrote a tombstone.
    linked: bool
    # The written row, echoed so the caller can chain. from/to are the
    # canonical direction (a reversed verb such as blocked_by is flipped).
    from_key: str
    # The edge's object endpoint as stored, after any direction flip.
    to_key: str
    relation: str  # canonical verb as stored
    # The relationship confidence as stored, echoed.
    confidence: float = 1.0
    # relation == contradicts and both endpoints are live memories: both rows
    # are now status='conflicted' and appear in conflicts.list.
    conflict_opened: bool = False
    # The caller passed no relation; the row was stored as `related_to`.
    # Rides the ledger as out_relation_defaulted.
    relation_defaulted: bool = False


class MemoryAssignCategoryInput(BaseModel):
    """Link a memory to one of the namespace's registered categories.
    Many-to-many — a memory can be in multiple categories.

    Idempotent: re-assigning is a no-op (ReplacingMergeTree by
    created_at). Pass `tombstone=True` to rescind a prior link."""

    org: str
    namespace: str
    # The memory to categorise; the key is stored as given, not checked for a live row.
    memory_key: str
    # A category registered in the namespace (namespace.register_category), else 404.
    category_name: str
    # True rescinds the memory's link to the category.
    tombstone: bool = False


class MemoryAssignCategoryOutput(BaseModel):
    # True when the link was written; False when it was rescinded.
    assigned: bool
    # The id of the named category.
    category_id: str
