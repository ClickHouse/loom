"""Namespace wire models for the reads that describe what a namespace holds:
its taxonomy, categories and ontology, and the summary, validate and audit
reports. The registry and membership models are in models.py."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from server.identity.api.models_findings import FindingContract, ReportCoverage


class NamespaceTaxonomyInput(BaseModel):
    """The de-facto taxonomy of a namespace — tags + entities across its
    memories. Lets a writing agent reuse existing vocabulary instead of
    inventing new tags, reducing topic drift across sessions.
    """

    org: str
    namespace: str
    # Top-N by frequency descending; rare values are cut.
    top_n_tags: int = Field(default=50, ge=1, le=500)
    # Most frequent entities to return; write-path sentinel values are left out.
    top_n_entities: int = Field(default=50, ge=1, le=500)
    # Largest entity communities to return (0 = skip the lookup).
    top_n_communities: int = Field(default=12, ge=0, le=100)


class _TaxonomyEntry(BaseModel):
    value: str
    count: int


class _TaxonomyCommunity(BaseModel):
    """One cluster of the namespace's entity co-occurrence graph: a topic the namespace
    is about, named by its highest-degree member until a summary exists. Empty list =
    the entity_communities worker has not run for this namespace."""

    community_id: int
    label: str
    size: int
    top_entities: list[str] = Field(default_factory=list)


class NamespaceTaxonomyOutput(BaseModel):
    # The most frequent tags on live rows, most used first, at most `top_n_tags`.
    tags: list[_TaxonomyEntry]
    # The most frequent entities on live rows, most used first, at most `top_n_entities`.
    entities: list[_TaxonomyEntry]
    # Live (non-tombstoned) rows in the namespace.
    memory_count: int
    # memory_types present in the namespace, with counts.
    memory_types: list[_TaxonomyEntry]
    # Topic clusters from the entity graph, largest first; [] until the
    # entity_communities worker has run.
    communities: list[_TaxonomyCommunity] = Field(default_factory=list)
    # When the returned communities were computed; empty when none were returned.
    communities_computed_at: str = ""


class NamespaceRegisterCategoryInput(BaseModel):
    """Register a category in a namespace. Its description + summary are
    embedded at registration so agents can cosine-search the registry via
    `namespace.search_categories`. Re-registering by name updates in place."""

    org: str
    namespace: str
    # The category's name; its id derives from it, so registering a name again updates it.
    name: str
    # One line on what belongs in the category; embedded with the name and summary.
    description: str = ""
    # Longer text embedded with the name; search hits show its first 280 characters.
    summary: str = ""


class NamespaceRegisterCategoryOutput(BaseModel):
    # Stable 32-hex id derived from org, namespace and name.
    category_id: str
    # False when the embedder failed: the category is stored but `search_categories`
    # never returns it.
    embedded: bool


class NamespaceListCategoriesInput(BaseModel):
    org: str
    namespace: str


class _CategoryEntry(BaseModel):
    category_id: str
    name: str
    description: str
    item_count: int


class NamespaceListCategoriesOutput(BaseModel):
    # Live categories, most linked memories first, then by name.
    categories: list[_CategoryEntry]


class NamespaceSearchCategoriesInput(BaseModel):
    org: str
    namespace: str
    # Text embedded and compared with each category's embedding.
    query: str
    # At most this many categories, nearest first; 1 to 20.
    top_k: int = Field(default=5, ge=1, le=20)


class _CategoryHit(BaseModel):
    category_id: str
    name: str
    description: str
    summary_excerpt: str
    distance: float
    similarity: float


class NamespaceSearchCategoriesOutput(BaseModel):
    # Embedded categories nearest to the query first; empty when the query could not be
    # embedded.
    results: list[_CategoryHit]


class NamespaceSummaryInput(BaseModel):
    """Aggregated read-only metadata for one namespace, for a single-shot
    dashboard tile (category/memory counts, top entities, recent activity)."""

    org: str
    namespace: str
    # At most this many entities in `top_entities`; 0 skips the lookup.
    top_entities_limit: int = Field(default=10, ge=0, le=100)
    # The window the activity counts cover, in seconds; 1 minute to 30 days.
    activity_seconds: int = Field(default=86400, ge=60, le=30 * 24 * 3600)


class _NamespaceTypeCount(BaseModel):
    memory_type: str
    n: int


class _NamespaceTopEntity(BaseModel):
    entity: str
    degree: int


class NamespaceSummaryOutput(BaseModel):
    org: str
    namespace: str
    # Live (non-tombstoned) rows in the namespace.
    total_memories: int
    # Live rows per memory type, most first.
    type_counts: list[_NamespaceTypeCount]
    # Entities with the highest co-occurrence degree over live graph edges.
    top_entities: list[_NamespaceTopEntity]
    recent_writes: int  # memory write calls in the activity window
    recent_recalls: int  # memory.search calls in the window
    # Distinct agents with a ledger row in the activity window; the dashboard's and the
    # assistant toolset's calls are left out, as they are of the recalls above, except a
    # change to a memory (a set, a delete, a pin).
    distinct_agents: int
    # Time of the newest ledger row in the activity window, the same calls left out.
    last_activity_at: datetime | None


# `attribute`: a memory-key attribute (`pet`, `employer`, `pet_name`); with
# `cardinality` it says whether a subject holds one value or many for it.
_OntologyKind = Literal["entity_type", "relation_verb", "attribute"]


_OntologyStatus = Literal["accepted", "pending", "tombstoned"]


_RelationCardinality = Literal["", "one", "many"]


class NamespaceRegisterOntologyInput(BaseModel):
    """Register an entity type or relation verb for a namespace.

    The extractor reads the accepted set and prefers registered names (cuts
    synonym sprawl). Names outside the set surface as soft `ontology_warnings`
    on the write — never reject, so the discovery flow keeps working."""

    org: str
    namespace: str
    # `entity_type`, `relation_verb` or `attribute` (a memory-key attribute).
    kind: _OntologyKind
    # The name the extractor should prefer; registering it again replaces the entry.
    name: str = Field(min_length=1, max_length=200)
    # Free text on what the name means.
    description: str = ""
    # `accepted` names are read by the extractor; `pending` ones are on file only;
    # `tombstoned` retires a name.
    status: _OntologyStatus = "accepted"
    # relation_verb and attribute. "one": a subject holds ONE current object — a newer
    # value closes the older interval on write (employer, location, status).
    # "many": never auto-closed, even if the seed list would ("addresses" the
    # verb vs the postal address). "" (default): the seed list decides.
    cardinality: _RelationCardinality = ""


class NamespaceRegisterOntologyOutput(BaseModel):
    # Always true once the entry is written.
    registered: bool
    org: str
    namespace: str
    # The kind registered (echoes the request).
    kind: str
    # The name registered (echoes the request).
    name: str
    # The status stored (echoes the request).
    status: str


class NamespaceListOntologyInput(BaseModel):
    org: str
    namespace: str
    kind: _OntologyKind | None = None  # None = both
    # Only entries with this status; None lists every status.
    status: _OntologyStatus | None = "accepted"


class _OntologyEntry(BaseModel):
    kind: str
    name: str
    status: str
    description: str
    cardinality: str = ""
    created_by: str
    updated_at: datetime


class NamespaceListOntologyOutput(BaseModel):
    # The matching entries, ordered by kind, then name.
    entries: list[_OntologyEntry]
    org: str
    namespace: str


_ValidateSeverity = Literal["error", "warning", "info"]


class NamespaceValidateInput(BaseModel):
    """Integrity report for one namespace, in one call, shaped for the agent
    that just wrote to it.

    Every check is a query over existing tables (no new storage, no
    mutation). Each returns a count and up to `sample_limit` sample rows so
    the caller can act on a finding without a second round-trip. A check
    that cannot run (table absent, query error) is listed in `skipped`, never
    raised — the report is advisory and must not fail the task it closes.

    `strict=True` makes `healthy` require zero warnings as well as zero
    errors (a CI gate); the default treats warnings as advisory.
    """

    org: str
    namespace: str
    # Sample rows per finding, 0 to 50; 0 lists none and still counts every finding.
    sample_limit: int = Field(default=5, ge=0, le=50)
    # A conflicted row older than this is a review that never happened.
    conflict_age_seconds: int = Field(default=7 * 86400, ge=0, le=365 * 86400)
    # Rows below this confidence with no source pointer are the agent's own
    # unqualified inferences — reported as info, not error.
    confidence_floor: float = Field(default=0.5, ge=0.0, le=1.0)
    # A memory without a vector younger than this is the embedding worker's
    # normal lag, not a finding.
    embed_grace_seconds: int = Field(default=600, ge=0, le=86400)
    # True makes `healthy` require zero warnings as well as zero errors.
    strict: bool = False
    # Scope every check to these memory keys — the keys one
    # session wrote — so a Stop hook can run the report per task at O(session)
    # cost. Empty (the default) is the whole-namespace report.
    keys: list[str] = Field(default_factory=list, max_length=200)


class _NamespaceValidateFinding(FindingContract):
    check: str
    severity: _ValidateSeverity
    count: int
    # Up to `sample_limit` rows; keys vary per check (memory_key always present).
    samples: list[dict[str, Any]] = Field(default_factory=list)
    # One sentence: what the check means and what to do about a hit.
    detail: str


class NamespaceValidateOutput(BaseModel):
    org: str
    namespace: str
    # errors == 0 (and warnings == 0 when strict).
    healthy: bool
    # Rows flagged by error-severity checks, summed across findings.
    errors: int
    # Rows flagged by warning-severity checks, summed across findings.
    warnings: int
    # Rows flagged by info-severity checks, summed across findings.
    infos: int
    # One entry per check that ran, including checks whose count is 0.
    findings: list[_NamespaceValidateFinding]
    # Rules that could not run (checks, the evidence read), as "name: reason".
    skipped: list[str] = Field(default_factory=list)
    # How many keys the report was scoped to (0 = whole
    # namespace), so a caller can tell a per-task report from a full one.
    keys_scoped: int = 0
    # Which rules ran and which were skipped: the checks and the evidence read.
    coverage: ReportCoverage
    # When the report started (UTC, to the second): a cited revision updated at or after
    # it may have changed mid-report.
    checked_at: datetime


_AuditSeverity = Literal["info", "warning", "critical"]


class NamespaceAuditInput(BaseModel):
    """Usage report for one namespace: is it being used as a memory, or as an
    object store? Every check is one read over existing tables (loom.memory,
    loom.memory_embeddings, loom.memory_versions, loom.search_trace_log,
    loom.agent_events); nothing is written. A check that cannot run lands in
    `skipped`, never raises. Each finding names the anti-pattern, how much of
    the namespace it covers, and the recipe remedy.
    """

    org: str
    namespace: str
    # Ledger window for the read/search counts and the hot-key join.
    window_seconds: int = Field(default=7 * 86400, ge=60, le=90 * 86400)
    # Sample rows per finding, 0 to 50; samples carry keys and counts, never values.
    sample_limit: int = Field(default=5, ge=0, le=50)


class NamespaceAuditSummary(BaseModel):
    live_rows: int = 0
    live_bytes: int = 0
    # Bytes of content_excerpt the embedding rows carry (a copy of each value's head).
    excerpt_bytes: int = 0
    # The versions whose content differs from the previous version of the same row (a
    # status-only version repeats it and is not counted), and their bytes.
    version_rows: int = 0
    version_bytes: int = 0
    # Searchable-type embedding rows with no vector and an embed error of their own (a
    # model call the deployment held or could not bill is not counted: index_backlog).
    unembedded_rows: int = 0
    # Ledger counts over `window_seconds`.
    gets: int = 0
    searches: int = 0
    sets: int = 0
    # (gets - typed_gets) / max(searches, 1): the object-store gauge; reads of procedures
    # are left out.
    get_search_ratio: float = 0.0
    # The gets and searches in the window from the dashboard and the assistant
    # toolset (consumers `loom-ui`, `loom-assistant`), left out of the counts above.
    inspector_calls: int = 0
    # Exact-key reads of rows whose type is structured by design (procedures): counted in
    # `gets`, left out of the object-store gauge.
    typed_gets: int = 0
    # Live rows created in the window (not in its last minute, which the ledger may not
    # have caught up with), and the ledger's calls for the namespace in it, apart from
    # reports on it and inspector reads: rows created with no calls mean the counts above
    # were not recorded.
    written_in_window: int = 0
    ledger_calls: int = 0


class NamespaceAuditFinding(FindingContract):
    code: str
    severity: _AuditSeverity
    count: int
    # 0..1 of the population the check looked at (live rows, searchable rows,
    # searches in the window, ...).
    share: float = Field(ge=0.0, le=1.0)
    bytes: int = 0
    # Keys and counts only — never a value. For queries, the redacted shape
    # (`sha256:<hex>/<n>`).
    samples: list[dict[str, Any]] = Field(default_factory=list)
    detail: str
    remedy: str


class NamespaceAuditOutput(BaseModel):
    org: str
    namespace: str
    # When the report started (UTC, to the second): a cited revision updated at or after
    # it may have changed mid-report.
    computed_at: datetime
    # The ledger window the counts cover, in seconds (echoes the request).
    window_seconds: int
    # Size and access totals of the namespace the findings are measured against.
    summary: NamespaceAuditSummary
    # One entry per check that ran; a clean check is an `info` finding with count 0.
    findings: list[NamespaceAuditFinding]
    # Rules that could not run (checks, summary reads, the evidence read), as "name: reason".
    skipped: list[str] = Field(default_factory=list)
    # Which rules ran and which were skipped: checks, summary reads, the evidence read.
    coverage: ReportCoverage
    # No critical finding.
    healthy: bool
    # Findings graded `critical`.
    criticals: int
    # Findings graded `warning`.
    warnings: int
    # Findings graded `info`, clean checks included.
    infos: int
