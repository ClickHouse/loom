"""Facts plane — query the `(subject, relation, object)` triples the
extractor emits into `loom.entity_triples` (aggregated by triple) and
`loom.entity_triple_memories` (triple → source memory_keys).

Surface: list / related / traverse / path / expire_by_adapter. Handlers
live in `server/memory/app/facts.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


# ============================================================
# facts.list
# ============================================================


class FactsListInput(BaseModel):
    org: str
    namespace: str
    # Only triples with this subject, canonicalised like a write (or as given, lowered);
    # subject/object/relation AND together, and unfiltered returns top triples by co_occur.
    subject: str | None = None
    # Only triples with this object, canonicalised like a write (or as given, lowered);
    # ignored when `entity` is set.
    object: str | None = None
    # Only triples with this relation, normalised the way writes store it (`works_at` ->
    # `work_at`).
    relation: str | None = None
    # Triples with this subject or object, canonicalised like a write (or as given,
    # lowered). Wins over subject/object when both are passed.
    entity: str | None = None
    # At most this many triples; the server caps it at 500.
    limit: int = Field(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT)
    # `co_occur` ranks by observation count, then recency; `recency` by `last_seen` first.
    sort_by: Literal["co_occur", "recency"] = "co_occur"
    # Fan out each triple to source memory_keys — extra CH round-trip, off by default.
    include_sources: bool = False
    # With the keys, read each source row's status (one more round-trip) and
    # flag a triple that is open while every row behind it is superseded —
    # the triple plane lagging a row update (writes close these at write time;
    # this read shows any that remain).
    include_source_status: bool = False
    # Drop triples observed fewer than this many times (summed over their rows).
    min_co_occur: int = Field(default=1, ge=1)
    # Only triples whose (valid_from, valid_to) interval contains this timestamp.
    as_of: datetime | None = None
    # Provenance tiers to keep: any of "extracted", "inferred",
    # "facet". None = every tier. Older rows carry ''
    # and are read as inferred.
    provenance: list[Literal["extracted", "inferred", "facet"]] | None = None


class FactRow(BaseModel):
    subject: str
    relation: str
    object: str
    co_occur: int
    last_seen: datetime
    # "extracted" (both endpoints in the carrier text),
    # "inferred", or "facet" (a loom-minted navigation edge); "" when no tier
    # was recorded. The strongest tier among the rows behind this triple.
    provenance: str = ""
    # Only when include_sources=True; bounded to ~10 keys per triple.
    source_keys: list[str] = Field(default_factory=list)
    # Only when include_source_status=True: each source key's row status
    # ("active", "superseded", "duplicate", …) and whether the triple is open
    # with no active row behind it.
    sources: list[SourceRef] = Field(default_factory=list)
    stale: bool = False


class SourceRef(BaseModel):
    memory_key: str
    status: str


class FactsListOutput(BaseModel):
    # The matching triples in `sort_by` order. Without `as_of`, only triples still valid now.
    rows: list[FactRow]
    # True when more triples matched than `limit`.
    truncated: bool = False


# ============================================================
# facts.related
# ============================================================


class FactsRelatedInput(BaseModel):
    org: str
    namespace: str
    # The entity whose neighbours to list, matched as subject or object: as the write path
    # stores it (canonicalised) or as given, lowered.
    entity: str
    # At most this many neighbours; the server caps it at 200.
    top_k: int = Field(default=20, ge=1, le=200)
    # Only edges valid at this time; `None` keeps the edges still valid now.
    as_of: datetime | None = None


class _RelatedEntity(BaseModel):
    entity: str
    co_occur: int
    # The seed's role relative to this neighbor; "both" when edges run both ways.
    role: Literal["subject", "object", "both"]


class FactsRelatedOutput(BaseModel):
    # `entity` canonicalised like a write, or "" when it has no canonical form.
    entity: str
    # Neighbours of `entity` over any relation, by summed `co_occur` descending.
    related: list[_RelatedEntity]


# ============================================================
# facts.traverse — N-hop graph walk via recursive CTE
# ============================================================


class FactsTraverseInput(BaseModel):
    """Walk the entity co-occurrence graph N hops out from a seed, one
    bounded query per hop. At each hop every frontier
    entity keeps its `top_k_per_hop` strongest unvisited neighbours and
    only those expand further, so the walk holds at most K^hops nodes,
    and never more than `max_nodes` in total. Hop weight accumulates
    (1/hop) so nearer neighbors rank higher; each entity is reported once,
    at the first hop that reached it.
    """

    org: str
    namespace: str
    # Entity to walk from; canonicalised like the write path (articles, possessives,
    # plurals) before the lookup.
    seed_entity: str
    # How many hops out from the seed to walk (1–4).
    max_hops: int = Field(default=2, ge=1, le=4)
    # Neighbours each frontier entity keeps per hop, strongest first (1–100).
    top_k_per_hop: int = Field(default=20, ge=1, le=100)
    # Ignore edges whose summed observation count is below this.
    min_co_occur: int = Field(default=1, ge=1)
    # Hard ceiling on returned nodes across all hops. When a hop would cross
    # it, that hop keeps its strongest pairs up to the ceiling and the walk
    # stops; the output reports `truncated=True`.
    max_nodes: int = Field(default=400, ge=1, le=5000)
    # Only edges valid at this time; `None` keeps the edges still valid now.
    as_of: datetime | None = None
    # Walk the TYPED graph (loom.entity_triples: subject -relation-> object,
    # with provenance in entity_triple_memories) instead of the co-occurrence
    # table. `relations` restricts the walk to those labels (canonical,
    # lower_snake); `labeled=True` walks every typed edge. Either switches
    # the edge set; each returned node then carries `via_relation`, the label
    # of the edge that reached it, and `via_direction` ("out" = seed side is
    # the subject, "in" = seed side is the object). Default off: the
    # co-occurrence walk.
    relations: list[str] = Field(default_factory=list, max_length=20)
    # True walks every typed edge (see `relations` above); False walks co-occurrence.
    labeled: bool = False
    # Hub-expansion policy. A reached entity whose total
    # co-occurrence degree (loom.entity_degree) is at or above this value is
    # REPORTED at the hop that reached it but is NOT expanded: it never joins
    # the next frontier, so its generic neighbourhood ("user", "pr", "file")
    # cannot fill the later hops. The seed always expands — a caller who
    # wants a hub's own neighbourhood seeds the walk from it. 0 = expand
    # everything (the plain co-occurrence walk), as is None. Namespace-specific:
    # read the degree tail first (on one namespace 500 was the 96th percentile).
    hub_degree: int | None = Field(default=None, ge=0)
    # Relative hub rule: a reached node is a hub when the
    # edge that reached it explains at most this share of the node's own
    # degree — `via_co_occur / degree`, the probability of the parent given
    # the node. Unlike `hub_degree` it
    # does not depend on the namespace's degree scale and does not hold a
    # seed's strong, specific pairs. Either rule holding is enough. 0 = off;
    # None = the deployment default `LOOM_FACTS_TRAVERSE_HUB_SHARE`.
    hub_share: float | None = Field(default=None, ge=0.0, le=1.0)
    # What the per-hop trim ranks by. `co_occur` keeps the
    # K pairs with the most co-mentions; `specificity` keeps the K
    # pairs whose co-mentions are the largest share of the neighbour's own
    # degree (via_co_occur / degree — within one parent this orders like
    # PMI), so a hop keeps the neighbours the parent explains rather than
    # the namespace's biggest nodes; `cosine` is the middle ground
    # (via_co_occur / sqrt(degree)). None = the deployment default
    # `LOOM_FACTS_TRAVERSE_RANK_BY`.
    rank_by: Literal["co_occur", "specificity", "cosine"] | None = None


class _TraversalNode(BaseModel):
    entity: str
    hop: int
    cumulative_weight: float
    via_co_occur: int  # the strongest edge that brought this node in
    # True when the hub policy reported this node without
    # expanding it (degree >= hub_degree, or via_co_occur / degree <=
    # hub_share). Its own neighbours are absent from the
    # walk by choice, not because the graph ends here.
    hub: bool = False
    # The node's total co-occurrence degree in the namespace (entity_degree)
    # as the hub policy or the specificity trim read it; 0 when neither ran.
    degree: int = 0
    via_relation: str = ""  # typed walk only: the label of that edge
    via_direction: str = ""  # typed walk only: "out" | "in" relative to the parent
    # Typed walk only: provenance tier of the edge that reached
    # this node — "extracted", "inferred" or "facet"; "" on the co-occurrence walk.
    via_provenance: str = ""
    # The validity window of the edge that reached this node:
    # ISO text, empty when the edge carries no bound. The facts block prints
    # them so the reader sees when a fact held, not only that it holds.
    valid_from: str = ""
    valid_to: str = ""
    # Community of the node in the latest entity_communities pass; None when the worker
    # has not run or the entity is unclustered.
    community_id: int | None = None
    # The chain that reached this node, seed first, this entity last;
    # hop == len(path) - 1. Lets a reader draw the walk
    # as a tree instead of a star, and lets an agent cite the route.
    path: list[str] = Field(default_factory=list)


class FactsTraverseOutput(BaseModel):
    seed: str  # the entity actually walked from (canonicalised; see handler)
    # Community of the seed in the latest community pass; `None` when unclustered.
    seed_community_id: int | None = None
    nodes: list[_TraversalNode]  # ordered by hop ASC, then cumulative_weight DESC
    # True when `max_nodes` cut the walk short.
    truncated: bool = False
    # Entities the hub policy reported but did not expand,
    # in the order the walk met them; empty when the policy is off or no
    # node crossed the threshold.
    hubs: list[str] = Field(default_factory=list)


# ============================================================
# facts.path — a path between two named entities
# ============================================================


class FactsPathInput(BaseModel):
    """Find how two entities are connected: a bounded bidirectional
    breadth-first search over the same validity-filtered edge set
    `facts.traverse` walks, meeting in the middle. `max_hops` bounds the
    TOTAL path length (a ... b), not the depth from one end.
    """

    org: str
    namespace: str
    # One end of the path; canonicalised like the write path before the search.
    entity_a: str = Field(min_length=1, max_length=500)
    # The other end; canonicalised the same way.
    entity_b: str = Field(min_length=1, max_length=500)
    # Longest path to look for, counted end to end (1–4).
    max_hops: int = Field(default=3, ge=1, le=4)
    # Neighbours each frontier entity keeps per hop, strongest first (1–100).
    top_k_per_hop: int = Field(default=20, ge=1, le=100)
    # Ignore edges whose summed observation count is below this.
    min_co_occur: int = Field(default=1, ge=1)
    # Only edges valid at this time; `None` keeps the edges still valid now.
    as_of: datetime | None = None
    # Typed graph (loom.entity_triples) instead of co-occurrence: `relations`
    # restricts to those labels, `labeled=True` walks every typed edge. Each
    # returned edge then carries `relation` and `direction` ("out" = entity_a
    # is the subject). Same switch, same meaning as facts.traverse.
    relations: list[str] = Field(default_factory=list, max_length=20)
    # True walks every typed edge (see `relations` above); False walks co-occurrence.
    labeled: bool = False


class _PathEdge(BaseModel):
    entity_a: str  # the end nearer `entity_a` of the request
    entity_b: str  # the end nearer `entity_b`
    relation: str = ""  # typed walk only
    direction: str = ""  # typed walk only: "out" | "in" relative to entity_a
    co_occur: int  # observations of this pair inside the validity window
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class FactsPathOutput(BaseModel):
    entity_a: str  # canonical form searched
    # Canonical form searched for the other end.
    entity_b: str
    # True when a path within `max_hops` exists; also True when both ends canonicalise alike.
    found: bool
    path: list[str]  # entity_a ... entity_b when found; [] otherwise
    edges: list[_PathEdge]  # len(path) - 1 when found
    hops: int  # len(edges) when found; hops explored when not
    # True when the answer may be incomplete: the hop budget ran out with
    # unexplored frontier, or per-hop top-k trimming dropped neighbours on
    # the way. False + not found = the two entities are not connected
    # within `max_hops` on qualifying edges.
    truncated: bool


# ============================================================
# facts.expire_by_adapter
# ============================================================


class FactsExpireByAdapterInput(BaseModel):
    """Tombstone graph-level triples emitted by a specific extractor
    (e.g. a buggy adapter) without touching the parent memories.
    """

    org: str
    namespace: str
    # Extractor adapter whose triples to delete (exact match on `adapter_name`).
    adapter_name: str
    # Confidence ceiling: only expire triples with confidence < threshold. 1.0 = no ceiling.
    max_confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FactsExpireByAdapterOutput(BaseModel):
    # Rows removed from `loom.entity_triples`; 0 when none matched.
    expired_triples: int
    # Rows removed from `loom.entity_triple_memories` (triple -> source key links).
    expired_triple_memories: int
