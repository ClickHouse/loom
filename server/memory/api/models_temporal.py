"""memory.* temporal / evidence ops — timeline, history, outcome."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from server.memory.api.models_common import MemoryType, _Identity

# ============================================================
# memory.history — list version history for a memory_key
# ============================================================


class MemoryDigestsInput(_Identity):
    """List the session digests of a namespace. `session_id`
    "*" (the default of a namespace audit) or "-" lists every session; a concrete
    session id lists that one."""

    # One session's digests; "*" or "-" = every session.
    session_id: str = "*"
    # At most this many digests, oldest session first; `truncated` says more exist.
    limit: int = Field(default=200, ge=1, le=2000)


class _DigestRow(BaseModel):
    session_id: str
    # Empty when the digest reads as high.
    summary_text: str
    # Key facts that read as high are left out.
    key_facts: list[str] = Field(default_factory=list)
    # memory keys the digest compressed (its lineage)
    derived_from: list[str] = Field(default_factory=list)
    valid_at: datetime | None = None
    created_at: datetime | None = None
    version: int = 0


class MemoryDigestsOutput(BaseModel):
    # The live (not tombstoned) session digests, ordered by creation time then session id.
    rows: list[_DigestRow]
    # True when more digests matched than `limit` returned.
    truncated: bool = False


class MemoryOutlineInput(_Identity):
    """A namespace's table of contents, read from structure alone: row counts by type,
    the dated sessions with their digest's first line, the hub entities by degree, the
    communities when a pass has computed them, and whether a consolidated profile exists
    for `user_id`. No model call; the read an agent makes before it writes."""

    # At most this many sessions, newest first; 0 skips the session read.
    sessions_limit: int = Field(default=50, ge=0, le=500)
    # At most this many hub entities, highest degree first; 0 skips the read.
    hubs_limit: int = Field(default=12, ge=0, le=100)
    # At most this many communities from the latest pass, largest first; 0 skips it.
    communities_limit: int = Field(default=8, ge=0, le=50)


class _OutlineSession(BaseModel):
    session_id: str
    date: str = ""
    # Empty when the digest reads as high.
    headline: str = ""
    key_facts: int = 0
    rows: int = 0


class _OutlineHub(BaseModel):
    entity: str
    degree: int = 0


class _OutlineCommunity(BaseModel):
    community_id: int
    label: str = ""
    size: int = 0
    computed_at: str = ""


class MemoryOutlineOutput(BaseModel):
    # Count of live, active rows per `memory_type`; document chunks are not counted.
    rows_by_type: dict[str, int] = Field(default_factory=dict)
    # Sessions with a digest, newest first: date, the digest's first sentence,
    # its key-fact count and how many memory keys it compressed.
    sessions: list[_OutlineSession] = Field(default_factory=list)
    # Number of live session digests in the namespace; 0 when `sessions_limit` is 0.
    sessions_total: int = 0
    # The entities with the most graph edges, with their degree.
    hubs: list[_OutlineHub] = Field(default_factory=list)
    # Top-level entity communities from the most recent community pass;
    # empty when no pass has run.
    communities: list[_OutlineCommunity] = Field(default_factory=list)
    # Length of `user_id`'s newest profile row; 0 when there is none or `user_id` is `-`.
    profile_chars: int = 0


class MemoryBacklinksInput(_Identity):
    """What points at one memory: its version chain, the
    digests whose lineage names it, the typed links in and out of it, how
    many evidence turns support it, and the memories that share its
    entities."""

    # The memory whose inbound references are read.
    memory_key: str
    # At most this many entries in each returned list.
    limit: int = Field(default=20, ge=1, le=200)


class _BacklinkVersion(BaseModel):
    version: int
    status: str = ""
    created_at: str = ""


class _BacklinkDigest(BaseModel):
    session_id: str
    date: str = ""


class _BacklinkEdge(BaseModel):
    memory_key: str
    relation: str = ""
    created_at: str = ""


class _BacklinkRelated(BaseModel):
    memory_key: str
    shared_entities: list[str] = Field(default_factory=list)


class MemoryBacklinksOutput(BaseModel):
    # Echo of the requested key.
    memory_key: str
    # True when the key has at least one stored version.
    found: bool = False
    # The key's versions, newest first, with status and write time.
    versions: list[_BacklinkVersion] = Field(default_factory=list)
    # Live session digests whose `derived_from` names this key, newest first.
    digests: list[_BacklinkDigest] = Field(default_factory=list)
    # Live operator links pointing at this key: the source key and relation.
    links_in: list[_BacklinkEdge] = Field(default_factory=list)
    # Live operator links from this key: the target key and relation.
    links_out: list[_BacklinkEdge] = Field(default_factory=list)
    # Distinct live evidence items (belief sources) that support this key.
    evidence_count: int = 0
    # Other memories sharing an entity with this key, most shared first
    # (up to four shared entities listed per memory).
    related: list[_BacklinkRelated] = Field(default_factory=list)


class MemoryHistoryInput(_Identity):
    """Enumerate the version history of one memory_key. Returns rows
    ordered newest-version-first, including tombstoned and redacted
    rows so callers can audit the full timeline."""

    # The key whose versions are listed.
    memory_key: str
    # At most this many versions, newest first.
    limit: int = Field(default=50, ge=1, le=500)


class _MemoryHistoryRow(BaseModel):
    memory_id: str
    version: int
    # The same version as a decimal string, exact for a JavaScript caller (the number is
    # above 2^53, which JSON.parse rounds): send this one to memory.redact. Set from
    # `version`.
    version_text: str = ""
    value: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    confidence: float
    source_trace: str = ""
    pinned: bool = False
    created_at: datetime
    expires_at: datetime | None = None
    tombstoned_at: datetime | None = None
    redacted_at: datetime | None = None
    redaction_reason: str = ""
    content_sha256: str = ""
    actor: str = ""
    source_class: str = ""
    # The writer agent of this version: a namespace-wide
    # history (agent "*") spans every client that wrote the key.
    agent: str = ""
    # When this version was written (`created_at` is the row's first write on
    # every version); the version is the write's epoch nanoseconds, so every
    # stored row has one.
    written_at: datetime | None = None
    # State a version can change with the value unchanged: a reader tells
    # such a version from the one before it by these.
    status: str = "active"
    conflict_with: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    verified_by: str = ""
    # The content hash the attestation covers: a re-attestation within the same
    # second by the same verifier differs from the one before only here.
    verified_sha256: str = ""
    lock_mode: str = ""
    lock_reason: str = ""
    lock_until: datetime | None = None

    @model_validator(mode="after")
    def _version_text(self) -> _MemoryHistoryRow:
        self.version_text = str(self.version)
        return self


class MemoryHistoryOutput(BaseModel):
    # Every recorded version of the key, newest first, tombstoned and redacted
    # ones included; empty when the key was never written.
    rows: list[_MemoryHistoryRow]


# ============================================================
# memory.timeline — temporal-ordered retrieval
# ============================================================


class MemoryTimelineInput(_Identity):
    """Return memories about an entity (or matching a tag filter) in strict
    temporal order — newest first, with optional since/until windows. Serves
    the temporal-reasoning case vector search can't.

    Sort key is coalesce(valid_at, created_at): operator-dated events position
    by valid_at, undated events by insertion time. At least one of `entity` /
    `tags` / `memory_key_prefix` must be set to scope the scan.
    """

    # Only memories tagged with this entity (matched lowercased); empty = no filter.
    entity: str = ""
    # Only memories carrying at least one of these tags; empty = no filter.
    tags: list[str] = Field(default_factory=list)
    # Only memories whose key starts with this; empty = no filter.
    memory_key_prefix: str = ""
    # Inclusive lower bound on `coalesce(valid_at, created_at)`; `None` = unbounded.
    since: datetime | None = None
    # Inclusive upper bound on `coalesce(valid_at, created_at)`; `None` = unbounded.
    until: datetime | None = None
    # At most this many events; `total_matching` counts them all.
    top_k: int = Field(default=50, ge=1, le=500)
    # `desc` returns newest first, `asc` oldest first.
    order: Literal["desc", "asc"] = "desc"


class _MemoryTimelineEntry(BaseModel):
    memory_key: str
    memory_type: MemoryType
    content_excerpt: str
    valid_at: datetime | None = None
    created_at: datetime
    namespace: str = ""


class MemoryTimelineOutput(BaseModel):
    # Matching private memories in time order, each with a content excerpt.
    events: list[_MemoryTimelineEntry]
    # Total matches before top_k truncation, so callers can detect "there's more".
    total_matching: int = 0


# ============================================================
# memory.outcome — usage-driven evidence bump
# ============================================================


class MemoryOutcomeInput(BaseModel):
    """External outcome signal for a memory row.

    Each positive outcome bumps `evidence_count`; a negative records a `harm`
    event — the caller relied on the row and the work ended badly — which the
    fold subtracts from the search-side reinforcement at
    LOOM_MEMORY_HARM_REINFORCEMENT_WEIGHT (default 0: counted, not charged),
    floored at the write-time base, so one bad reference takes back earned
    boost but never unlearns the row.
    `used` says the agent RELIED on the memory in its work — distinct from
    a recall (the search returned it) and from an outcome (the work ended
    well): it bumps the search-side reinforcement (weighted by
    LOOM_MEMORY_USED_REINFORCEMENT_WEIGHT), not the evidence gate.
    """

    org: str
    namespace: str
    # Address the row by stable id OR by memory_key (resolved server-side).
    # One of memory_id / memory_key is required. memory_key is convenient for
    # callers applying outcomes to keys they just recalled; agent defaults to
    # the "*" wildcard so a cross-agent recaller doesn't need the writer agent.
    memory_id: str = ""
    # The row's key, used when `memory_id` is empty; resolved under `agent`/`user_id`.
    memory_key: str = ""
    agent: str = "*"
    user_id: str = "-"
    # `positive` adds evidence, `negative` records harm (and counts toward the
    # auto-quarantine threshold), `used` adds search-side reinforcement.
    outcome: Literal["positive", "negative", "used"]
    # How many events this outcome counts as.
    weight: int = Field(default=1, ge=1, le=100)
    source: str = ""  # optional caller-supplied tag


class MemoryOutcomeOutput(BaseModel):
    # False when the row was not found or the usage write failed.
    recorded: bool
    evidence_count_after: int  # evidence_count after the bump (0 if unknown)
    # Set when a negative outcome crossed memory_negative_demote_threshold and
    # the memory was auto-quarantined (status -> 'rejected'); a locked memory is not.
    quarantined: bool = False


class MemoryOutcomeRunInput(BaseModel):
    """Apply one outcome to many recalled memories in a single call — the bulk
    form of memory.outcome, so callers skip a per-key loop.

    Provide the recalled keys explicitly in `memory_keys` (what you got back
    from search). If `memory_keys` is empty and `run_id` is set, the key set is
    derived from loom.search_trace_log for that run instead (requires the
    telemetry flag AND that the recalls were tagged with that run_id).
    """

    org: str
    namespace: str
    # `positive` records evidence, `negative` harm, `used` reinforcement on each key.
    outcome: Literal["positive", "negative", "used"]
    # The recalled keys to apply the outcome to; empty derives them from `run_id`.
    memory_keys: list[str] = Field(default_factory=list)
    # The run whose recalls supply the keys when memory_keys is empty; "" or "-" names
    # no run.
    run_id: str = ""
    # How many events the outcome counts as on each key.
    weight: int = Field(default=1, ge=1, le=100)
    # Optional caller-supplied tag; not stored on the usage events.
    source: str = ""
    # At most this many keys are applied; the rest are dropped.
    max_keys: int = Field(default=1000, ge=1, le=5000)


class MemoryOutcomeRunOutput(BaseModel):
    applied: int  # rows an event was recorded for (evidence / used / harm)
    keys_matched: int  # distinct keys recalled under the run
    run_id: str


# ============================================================
# memory.recall — the raw turns, by text and by time
# ============================================================


class MemoryRecallInput(_Identity):
    """Search the raw conversation turns behind this namespace's memories —
    the messages `memory.set_from_messages` extracted from — by text and by
    time. This is the read for what the extractor did not keep as a fact:
    the exact wording, the date a statement was made, a detail that became
    no memory. `memory.search` ranks facts; this returns turns.

    At least one of `query` (with a non-stop-word token), `since`/`until`,
    or `thread` is required so the read is scoped. `match="all"` requires
    every query token in a turn (precise); `"any"` admits a turn carrying
    any of them (broad, ranked by how many it carries). `order="auto"` is
    relevance when there is a query and newest-first otherwise. Newest-first
    pages by `cursor`; relevance pages by `offset`. `total_matched` and
    `has_more` say whether another page exists.
    """

    # Text to match in turns: lowercased tokens of 2+ characters, stop words dropped,
    # at most 32 kept; empty reads by time window or thread alone.
    query: str = Field(default="", max_length=2000)
    # Inclusive lower bound on the turn's observed time; `None` = unbounded.
    since: datetime | None = None
    # Inclusive upper bound on the turn's observed time; `None` = unbounded.
    until: datetime | None = None
    # Restrict to one conversation (the session_id the turns were ingested
    # under). Distinct from the identity `session_id`, which read scope binds.
    thread: str = Field(default="", max_length=200)
    # Only turns of this speaker role; empty returns turns of every role.
    role: Literal["", "user", "assistant"] = ""
    # `all`: a turn must carry every query token; `any`: at least one of them.
    match: Literal["all", "any"] = "all"
    # `recent` = newest first, `relevance` = most matched tokens first,
    # `auto` = relevance with a query, else recent.
    order: Literal["auto", "recent", "relevance"] = "auto"
    # At most this many turns per page.
    top_k: int = Field(default=20, ge=1, le=200)
    # `next_cursor` of the previous page in newest-first order; empty starts at the top.
    cursor: str = Field(default="", max_length=400)
    # Turns to skip in relevance order; pass the previous page's `next_offset`.
    offset: int = Field(default=0, ge=0, le=10_000)
    # Per-turn content cap; a long turn is cut and flagged `truncated`.
    max_chars: int = Field(default=600, ge=40, le=4000)
    # Same semantics as memory.search.read_scope.
    read_scope: Literal["namespace", "own_user", "own_session"] = "namespace"


class _RecallTurn(BaseModel):
    evidence_id: str
    session_id: str
    role: str = ""
    actor: str = ""
    observed_at: str = ""
    content: str = ""
    truncated: bool = False
    # Distinct query tokens this turn carries (0 when there was no query).
    matched_tokens: int = 0


class MemoryRecallOutput(BaseModel):
    # The matching turns of this page, each with content cut to `max_chars`.
    turns: list[_RecallTurn]
    # Turns matching the filter in the whole namespace scope, not just this
    # page: "showing 20 of 124".
    total_matched: int = 0
    # Number of turns in this page.
    returned: int = 0
    # True when another page exists after this one.
    has_more: bool = False
    # Newest-first paging token; empty when exhausted or in relevance order.
    next_cursor: str = ""
    # Relevance paging offset; -1 when exhausted or in newest-first order.
    next_offset: int = -1
    # The order actually used, after `auto` is resolved.
    order: Literal["recent", "relevance"] = "recent"
    # The tokens the query was reduced to and matched on.
    query_tokens: list[str] = Field(default_factory=list)
    # Estimated prompt tokens of the returned content (chars / 4).
    tokens_returned: int = 0
