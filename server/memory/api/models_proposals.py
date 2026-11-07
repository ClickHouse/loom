"""Fact proposals: list them with their graded
evidence, and accept or reject them.

A proposal is written with `memory.set(proposed=true)`; the read path serves
it only under `include_proposed`. `memory.proposals` answers "which facts are
on trial and how are they doing": each row carries the graded runs that
relied on it (from `run.grade`), the self-reported usage counters, and what
the namespace owner's policy thresholds say (`ready`). `memory.proposal_decide`
is the decision — a person's or an agent's; loom takes it on its own only
under an owner-set `proposal_policy.mode = "auto"`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from server.memory.domain.proposal_policy import ProposalPolicy


class MemoryProposalsInput(BaseModel):
    org: str
    namespace: str
    # Narrow the listing: a key prefix (`lesson_`), any-match tags.
    prefix: str = Field(default="", max_length=256)
    # Only proposals carrying any of these tags; empty = no tag filter.
    tags: list[str] = Field(default_factory=list, max_length=20)
    # `ready` keeps only proposals the policy thresholds would decide now
    # (accept or reject); "" lists every proposal.
    only: Literal["", "ready"] = ""
    # At most this many proposals returned, newest first; 0 returns only `total`.
    limit: int = Field(default=50, ge=0, le=500)


class MemoryProposal(BaseModel):
    memory_key: str
    memory_id: str
    # The listed row's version: a new proposal of the key by the same writer keeps the id.
    version: int = 0
    agent: str = ""
    # The writer's user and session ('-' when unset), which a memory.get of the key names.
    user_id: str = "-"
    session_id: str = "-"
    memory_type: str = ""
    # The value's first 400 characters; "" for a high row, read by key (memory.get).
    value_preview: str = ""
    # The value runs past the preview.
    preview_cut: bool = False
    # Sensitivity tier ('' | low | medium | high).
    sensitivity: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: str = ""
    # Graded runs that relied on it (run.complete memories_used + run.grade).
    graded_passes: int = 0
    graded_fails: int = 0
    last_graded_at: str = ""
    # Self-reported usage (run.complete / memory.outcome counters).
    used_count: int = 0
    harm_count: int = 0
    # What the namespace policy thresholds say now: accept / reject / "".
    ready: Literal["", "accept", "reject"] = ""
    # A contradiction recorded on trial (surfaced when accepted).
    conflict_with: list[str] = Field(default_factory=list)


class MemoryProposalsOutput(BaseModel):
    # Proposals a decision would take, newest first, with graded counts and `ready`: one
    # per key whose newest row is a proposal (an older proposal under a decided key is
    # left out).
    proposals: list[MemoryProposal] = Field(default_factory=list)
    # Keys whose newest row is a proposal the caller may read, before `only` and `limit`
    # (at most memory_proposals_scan_cap).
    total: int = 0
    # The namespace's proposal policy: `mode` (manual / auto) and thresholds.
    policy: ProposalPolicy = Field(default_factory=ProposalPolicy)


class MemoryProposalDecideInput(BaseModel):
    org: str
    namespace: str
    # Keys of the proposals to decide; a key that is not a proposal is left as is
    # and reported.
    memory_keys: list[str] = Field(min_length=1, max_length=500)
    # `accept` makes each proposal live (`active`, or `conflicted` when contradicted
    # on trial); `reject` sets it `rejected`.
    decision: Literal["accept", "reject"]
    # Why; stored as the rejection reason on `reject` ("proposal rejected" when empty).
    reason: str = Field(default="", max_length=512)


class MemoryProposalDecision(BaseModel):
    memory_key: str
    decided: bool
    # The row's status after the call ('' = no such key).
    status: str = ""
    reason: str = ""


class MemoryProposalDecideOutput(BaseModel):
    # How many keys were decided.
    decided: int = 0
    # One entry per key: decided or not, its status after, and why not.
    results: list[MemoryProposalDecision] = Field(default_factory=list)
