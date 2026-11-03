"""Decisions as first-class records.

`decision.record` writes a typed `decision` record (built-in schema), a
searchable memory row under the same key, and causal edges to what led to it.
`decision.precedents` is semantic search over past decisions, optionally
within a category. `decision.chain` walks caused / influenced / derived_from
edges upstream (what led here) and downstream (what this led to).
`decision.outcome` records what actually happened and credits the memories
the deciding run relied on. Handlers: server/memory/app/decision.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.core.naming import SENTINEL
from server.memory.api.models_common import FileRefs
from server.memory.domain.provenance import DeclaredSourceClass
from server.operations.base import MCP_HIDDEN


class DecisionRecordInput(BaseModel):
    org: str
    namespace: str
    agent: str
    user_id: str = Field(default=SENTINEL)
    session_id: str = Field(default=SENTINEL)
    # The run this decision was made in (run.begin). Joins the decision to the
    # ledger and lets decision.outcome credit the memories the run relied on.
    run_id: str = Field(default=SENTINEL)
    # category + scenario identify the decision: re-deciding the same scenario
    # supersedes the earlier record (history keeps it).
    category: str = Field(min_length=1, max_length=120)
    # The situation that needed deciding; `decision.precedents` matches a query against
    # the decision's statement, which opens with the scenario.
    scenario: str = Field(min_length=1, max_length=2000)
    # Why this outcome was chosen.
    reasoning: str = Field(min_length=1, max_length=4000)
    # What was decided.
    outcome: str = Field(min_length=1, max_length=1000)
    # What was considered and not chosen — the field every "why not X"
    # question needs and most decision logs omit. The record keeps each one up to 300
    # characters, a longer one cut at a space and ending in "…".
    alternatives: list[str] = Field(default_factory=list, max_length=10)
    # The decider's confidence, 0-1; 0 records none (the memory row and edges then
    # carry 1.0).
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # Who decided, a person or an agent; "" records the calling `agent`.
    decision_maker: str = Field(default="", max_length=200)
    # memory_keys (decisions, facts, procedures) that CAUSED or INFLUENCED this
    # decision; each becomes an authored edge (memory.link) with this call as
    # its evidence. Direction: cause → this decision.
    caused_by: list[str] = Field(default_factory=list, max_length=20)
    # Keys that shaped the decision without causing it; each becomes an `influenced`
    # edge.
    influenced_by: list[str] = Field(default_factory=list, max_length=20)
    # Extra tags on the memory row, beside `decision` and `decision:category:<category>`.
    tags: list[str] = Field(default_factory=list, max_length=16)
    # Real-world time the decision took effect; None = now.
    valid_at: datetime | None = None
    # The repo files this decision governs (repo-relative path or bare filename);
    # memory.for_file serves it before an edit of them. Unset = detected from the
    # decision's text; a rewrite that detects none keeps the row's; [] clears.
    files: FileRefs | None = None
    # Declare that this decision was derived from other content (`derived`) or came
    # from outside (`external`, `imported`). The server decides every other
    # class from the token and the operation; a body may only downgrade.
    source_class: DeclaredSourceClass | None = Field(default=None, json_schema_extra=MCP_HIDDEN)
    # The file path, document id or URL this decision was taken from, cited on its hits.
    source_resource_id: str = ""


class DecisionRecordOutput(BaseModel):
    # The decision's key, derived from category + scenario; pass it to
    # `decision.outcome` and `decision.chain`.
    memory_key: str
    # The id of the `decision` record version written.
    record_id: str
    # True when an active decision for the same category + scenario existed
    # and is now superseded by this one.
    superseded_prior: bool = False
    # How many `caused` / `influenced` edges were written; a failed link is skipped.
    edges_written: int = 0
    # Plain-text hint naming the follow-up operations.
    next: str = ""


class DecisionRow(BaseModel):
    memory_key: str
    category: str = ""
    scenario: str = ""
    reasoning: str = ""
    outcome: str = ""
    alternatives: list[str] = Field(default_factory=list)
    confidence: float | None = None
    decision_maker: str = ""
    run_id: str = ""
    realized_outcome: str = ""
    success: bool | None = None
    made_at: str = ""
    # Precedent search only: cosine similarity of the decision's statement to the query.
    similarity: float = 0.0


class DecisionPrecedentsInput(BaseModel):
    org: str
    namespace: str
    agent: str = "*"
    # The scenario at hand, phrased as the deciding agent sees it.
    query: str = Field(min_length=1, max_length=2000)
    # Only decisions in this category (case-insensitive); "" searches every category.
    category: str = Field(default="", max_length=120)
    # At most this many precedents, 1-25.
    top_k: int = Field(default=5, ge=1, le=25)
    # Only decisions whose realized outcome is known (decision.outcome called).
    with_outcome_only: bool = False


class DecisionPrecedentsOutput(BaseModel):
    # Active past decisions, the most similar decision statement first.
    precedents: list[DecisionRow]
    # The embedder the underlying memory search used.
    embedding_model: str = ""


class DecisionChainInput(BaseModel):
    org: str
    namespace: str
    # The key to start the walk from, usually a decision's.
    memory_key: str = Field(min_length=1)
    # `upstream` follows what led here, `downstream` what it led to, `both` walks each.
    direction: Literal["upstream", "downstream", "both"] = "both"
    # How many edges away to walk, 1-4.
    max_hops: int = Field(default=3, ge=1, le=4)


class ChainNodeRow(BaseModel):
    memory_key: str
    hop: int
    relation: str
    direction: str
    via: str
    # Populated when the node is itself a decision.
    decision: DecisionRow | None = None


class DecisionChainOutput(BaseModel):
    # The starting key, echoed.
    seed: str
    # The seed's decision record; null when the seed is not an active decision.
    seed_decision: DecisionRow | None = None
    # Every key reached over `caused`, `influenced` or `derived_from` edges, each once,
    # nearest hop first.
    nodes: list[ChainNodeRow]


class DecisionOutcomeInput(BaseModel):
    org: str
    namespace: str
    # The active decision to update; answers 404 when there is none, or when the record's
    # writer has no live decision row under the key.
    memory_key: str = Field(min_length=1)
    # What actually happened, stored on the decision record.
    realized_outcome: str = Field(min_length=1, max_length=2000)
    # None keeps the record's verdict; True/False sets it and credits (or marks) the
    # deciding run's memories.
    success: bool | None = None
    agent: str = Field(default="*")


class DecisionOutcomeOutput(BaseModel):
    # The decision's key, echoed.
    memory_key: str
    # Always true: a missing decision answers 404 instead.
    updated: bool
    # memories credited through the run ledger (run.complete memories_used).
    memories_credited: int = 0
