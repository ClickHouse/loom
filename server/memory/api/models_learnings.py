"""Learnings review queue — human-in-the-loop promotion of seeder
candidates into the institutional shared zone."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

LearningStatus = Literal["pending", "promoted", "rejected"]
ReviewDecision = Literal["promote", "reject"]


class LearningsListInput(BaseModel):
    org: str
    namespace: str
    status: LearningStatus | None = None  # None = pending
    # At most this many candidates, 1-500, newest proposal first.
    limit: int = Field(default=50, ge=1, le=500)
    source_kind: str | None = None  # None = all seeders


class _LearningEntry(BaseModel):
    candidate_id: str
    memory_key: str
    memory_type: str
    proposed_value: Any
    # How many back it; for `assistant_turn`, the distinct end users.
    supporter_count: int
    # `assistant_turn` only (0 otherwise): distinct (user, session) pairs that
    # stated the rule.
    supporter_sessions: int = 0
    confidence_avg: float
    supporters: list[str]
    rationale: str
    source_kind: str = "memory_supporters"
    # The agent an `assistant_turn` candidate is a rule for; '' for other producers.
    agent: str = ""
    status: LearningStatus
    proposed_at: datetime
    decided_at: datetime | None = None
    decided_by: str = ""
    decision_reason: str = ""


class LearningsListOutput(BaseModel):
    # Candidates with the requested status in `namespace`, newest proposal first.
    rows: list[_LearningEntry]


PromotionDestination = Literal["memory", "knowledge", "both"]


class LearningsReviewInput(BaseModel):
    org: str
    namespace: str
    # The queued candidate to decide; one unknown in `namespace` answers 404.
    candidate_id: str
    # `promote` writes it to `destination` and marks it promoted; `reject` only marks
    # it rejected.
    decision: ReviewDecision
    # Why; stored as `decision_reason` and carried into the promoted row's provenance.
    reason: str = ""
    # Where a promotion lands; unset is 'knowledge' for an assistant_turn rule and
    # 'memory' otherwise.
    destination: PromotionDestination | None = None
    # Captured into the audit subject + shared.promoted_by.
    approver: str = "learnings-reviewer"


class LearningsReviewOutput(BaseModel):
    # The candidate, echoed.
    candidate_id: str
    # The status the candidate holds after this call.
    new_status: LearningStatus
    # True when this call wrote memory: `loom.shared` for a supporters candidate, a
    # namespace row for a seeder, the agent's `user_id` '-' row for a bot's rule,
    # which reads bounded to one user do not serve.
    promoted_to_shared: bool = False
    # True when this call upserted the candidate as a knowledge document.
    promoted_to_knowledge: bool = False
    # source_uri the knowledge doc was upserted under (for later knowledge.delete).
    knowledge_source_uri: str = ""
    # When this call ran (UTC), also on a repeat that changed nothing.
    decided_at: datetime


# Per-namespace pending/promoted/rejected counts + approve-ratio for the
# Health view's Extraction Quality panel.
class LearningsQualityInput(BaseModel):
    org: str
    namespace: str | None = None  # None = org-wide rollup
    # Only candidates from this producer; None counts every producer. The default
    # counts the conversation seeder, the extraction quality the Health panel reports;
    # `assistant_turn` is a bot's rules about itself, not extraction quality.
    source_kind: str | None = "conversation_seeder"


class LearningsQualityRow(BaseModel):
    namespace: str
    pending: int
    promoted: int
    rejected: int
    approve_ratio: float  # promoted / (promoted + rejected); 0 when no decisions


class LearningsQualityOutput(BaseModel):
    # One row per namespace the caller may see, most pending first; empty without access.
    rows: list[LearningsQualityRow]
