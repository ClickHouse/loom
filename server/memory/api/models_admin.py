"""memory.* operator / maintenance ops — reindex, agents_list, consolidate, consolidations.list."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from server.memory.api.models_common import _Identity


class MemoryConsolidateInput(_Identity):
    """Periodic consolidation pass — merges fragmented memories about the same
    subject that slip past hash-dedup and extraction-time dedup.

    Algorithm:
      1. Fetch active memories for this identity (`limit` cap)
      2. Cluster by cosine similarity >= similarity_threshold AND entity overlap
      3. Per cluster, LLM-merge into one canonical memory (preserves unique facts)
      4. Write the merged memory; tombstone the merged-from keys; audit
      5. Return one entry per merge

    `dry_run=True` skips step 4 (preview only). Cost: one LLM call per cluster.
    """

    # At most this many of the identity's newest eligible rows are clustered.
    limit: int = Field(default=200, ge=10, le=1000)
    # Cosine at which two rows may cluster; they also need a shared entity unless
    # the cosine reaches max(threshold + 0.05, 0.92).
    similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    min_age_seconds: int = Field(
        default=3600,
        ge=0,
        description=(
            "Skip memories newer than this — protects in-flight writes "
            "from being consolidated away before retrieval can use them."
        ),
    )
    # True still runs the merge LLM calls but writes nothing: no canonical row, no
    # tombstones.
    dry_run: bool = False


class MemoryConsolidateResult(BaseModel):
    canonical_key: str
    canonical_value: str
    merged_keys: list[str]
    cluster_size: int


class MemoryConsolidateOutput(BaseModel):
    # One entry per cluster merged (or that would be, under `dry_run`).
    consolidated: list[MemoryConsolidateResult] = Field(default_factory=list)
    # Eligible rows read: live, unpinned, unlocked, embedded, older than
    # `min_age_seconds`, and neither labelled `high` nor reading as `high`
    # (never sent to the merge model).
    examined: int = 0
    # Clusters of two or more rows found.
    clusters_found: int = 0
    # Clusters whose merge LLM call failed or returned an unparseable /
    # incomplete JSON object. Each is logged and recorded in
    # loom.consolidation_runs with status='failed' (never silently skipped).
    clusters_failed: int = 0
    # Of the failed clusters, those the merge guard refused because the merged
    # text lost a date, quantity, identifier, name or negation one source
    # carried; the sources stay as they were.
    clusters_refused_lossy: int = 0
    # Always 0: rows newer than `min_age_seconds` are left out of the read, not
    # counted.
    skipped_in_flight: int = 0
    # Prompt tokens of the merge LLM calls.
    tokens_in: int = 0
    # Completion tokens of the merge LLM calls.
    tokens_out: int = 0


# ============================================================
# consolidations.list — history of memory-consolidation runs
# ============================================================


class ConsolidationsListInput(_Identity):
    """Namespace-scoped consolidation-run history. agent + user_id are
    optional — runs aggregate across them when omitted."""

    agent: str = "-"
    user_id: str = "-"
    # At most this many runs returned, newest first.
    limit: int = Field(default=50, ge=1, le=500)


class ConsolidationRunRow(BaseModel):
    run_id: str
    memory_type: str
    source_memory_ids: list[str]
    source_memory_keys: list[str]
    cluster_size: int
    cluster_cohesion: float
    output_memory_id: str
    output_memory_key: str
    llm_provider: str
    llm_model: str
    tokens_in: int
    tokens_out: int
    duration_ms: int
    status: str
    failure_reason: str
    ran_at: str


class ConsolidationsListOutput(BaseModel):
    # The namespace's consolidation runs, newest first, one per cluster examined.
    rows: list[ConsolidationRunRow]


# ============================================================
# memory.agents_list — distinct agent names for the UI agent picker
# ============================================================


class MemoryAgentsListInput(_Identity):
    """Distinct agent names that have ever written into (org, namespace).
    agent + user_id are forced sentinels — we aggregate ACROSS them to discover
    which agents exist."""

    agent: str = "-"
    user_id: str = "-"


class MemoryAgentsListOutput(BaseModel):
    # Distinct agent names with a live embedding row in the namespace, sorted.
    agents: list[str]


class MemoryHealthInput(_Identity):
    """Knowledge-base health for (org, namespace): how much of what is stored
    is ever recalled, what is silent, what earns its keep. agent + user_id are
    sentinels — the report aggregates across them."""

    agent: str = "-"
    user_id: str = "-"
    # A live row older than this with no recall credit and no agent read in this many days
    # is "silent"; also the window of agent reads counted.
    silent_after_days: int = Field(default=30, ge=1, le=3650)
    # How many entries `top_recalled` lists.
    top_k: int = Field(default=10, ge=1, le=50)


class MemoryHealthTypeRow(BaseModel):
    memory_type: str
    live: int
    recalled: int


class MemoryHealthTopRow(BaseModel):
    memory_key: str
    memory_type: str
    recall_count: int
    used_count: int
    # relied on in runs that ended badly
    harm_count: int = 0
    last_recalled_at: datetime | None = None


class MemoryHealthStaleness(BaseModel):
    """Staleness distribution of the live rows: how old they are, how many carry a
    validity end or an expiry, how many are pinned, and how long since ranking last saw
    them reinforced."""

    by_age: dict[str, int] = Field(default_factory=dict)  # lt7d / d7_30 / d30_90 / gt90d
    with_validity_end: int = 0  # valid_to or invalid_at set
    with_validity_end_share: float = 0.0
    with_expiry: int = 0
    expiring_7d: int = 0
    pinned: int = 0
    days_since_reinforced_p50: int = 0
    days_since_reinforced_p90: int = 0


class MemoryHealthFootprint(BaseModel):
    """Stored size of the live rows: summed value length and its ~4-chars-per-
    token estimate. An estimate, not a tokenizer count."""

    value_chars: int = 0
    value_tokens_est: int = 0
    tokens_per_row_est: float = 0.0


class MemoryHealthOutput(BaseModel):
    # Active, untombstoned rows in the namespace, across every agent and user.
    live: int
    # Live rows whose key carries a recall credit on any live row, or was returned by an agent
    # read (a search, context pack or memory.ask) in the last `silent_after_days` days.
    recalled_ever: int
    # Live rows with at least one reported use.
    used_ever: int
    harmed_ever: int = 0  # live rows with at least one harm event
    silent: int  # live, no recall credit, and no agent read in the last silent_after_days
    silent_older: int  # silent AND older than silent_after_days
    # `silent` / `live`, 0..1; 0 when the namespace holds no live row.
    silent_share: float
    # Echo of the request's `silent_after_days`.
    silent_after_days: int
    # Per memory type: live rows and how many are recalled (as `recalled_ever` counts them),
    # largest first.
    by_type: list[MemoryHealthTypeRow] = Field(default_factory=list)
    # The live rows with the most recall credits (then use count), at most `top_k`.
    top_recalled: list[MemoryHealthTopRow] = Field(default_factory=list)
    # Age buckets, validity-end, expiry and pin counts, and days since reinforced.
    staleness: MemoryHealthStaleness = Field(default_factory=MemoryHealthStaleness)
    # Summed value length of the live rows and its chars/4 token estimate.
    footprint: MemoryHealthFootprint = Field(default_factory=MemoryHealthFootprint)


# ============================================================
# memory.reindex — re-embed a namespace under the current embedding_model.
# Needed after an embedder switch: search filters by embedding_model, so
# rows embedded under the old model otherwise become invisible.
# ============================================================


class MemoryReindexInput(BaseModel):
    org: str
    namespace: str
    # Max rows per call (bounded to avoid spiking the embedder / holding CH locks).
    # Caller pages by re-running until processed < limit.
    limit: int = Field(default=200, ge=1, le=2000)
    # Also re-embed rows already matching the current model (force a clean rebuild).
    force: bool = False


class MemoryReindexOutput(BaseModel):
    # Rows re-embedded in this call; call again until it is below `limit`.
    processed: int
    # Always 0: rows already on the current model are not read unless `force`.
    skipped_already_current: int
    # The embedding model the rows were re-embedded under (the current one).
    target_model: str
    # The vector dimension of that model.
    target_dim: int


# ============================================================
# memory.resolve_orphan_duplicates — re-activate duplicates whose
# canonical is gone.
#
# A row is set status='duplicate' + duplicate_of=<peer> by
# conflicts.check_on_commit when it's a same-fact/different-words match of a
# LIVE peer: it stays in recall but defers to that canonical. If the canonical
# later disappears (duplicate_of never set, or it's now tombstoned / superseded
# / not a live active head), the row is an ORPHAN duplicate — stuck 'duplicate'
# forever with nothing live to defer to. conflicts.resolve only handles
# 'conflicted' and knowledge.dedup_resolve is the knowledge plane; both reject
# an empty canonical, so nothing re-activates these. This op does: it re-emits
# each orphan with status='active' (a standalone memory, not a duplicate of
# anything live). Idempotent — a re-activated row is no longer 'duplicate'.
# ============================================================


class MemoryResolveOrphanDuplicatesInput(BaseModel):
    """Scan (org, namespace) for orphaned duplicate rows and re-activate them.

    An orphan is a live active-head row with status='duplicate' whose
    duplicate_of is empty OR points at a memory that is no longer a live active
    head in this namespace. `dry_run=True` returns the candidates without
    writing anything.
    """

    org: str
    namespace: str
    # Max orphan rows to re-activate per call. Caller pages by re-running (the
    # op is idempotent — re-activated rows drop out of the scan).
    limit: int = Field(default=500, ge=1, le=5000)
    # True lists the orphan candidates without re-activating them.
    dry_run: bool = False


class MemoryResolveOrphanDuplicatesOutput(BaseModel):
    # Orphan duplicates the scan matched.
    candidates: int = 0
    # Rows re-emitted status='active' (0 under dry_run).
    reactivated: int = 0
    # Echo of the request's `dry_run`.
    dry_run: bool = False
    # memory_ids of the affected (candidate) rows.
    memory_ids: list[str] = Field(default_factory=list)
