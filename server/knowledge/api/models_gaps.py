"""Wire models of `knowledge.gaps.list`."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeGapsListInput(BaseModel):
    """The questions people keep asking in a namespace that nothing answers.

    No memory or document answered them; the daily knowledge_gaps worker groups them.
    A query appears only once enough distinct askers asked it in that form
    (LOOM_KNOWLEDGE_GAPS_MIN_ASKERS), and only where the operator turned recording on
    (LOOM_KNOWLEDGE_GAPS_ENABLED)."""

    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The namespace whose gaps to list.")
    limit: int = Field(default=50, ge=1, le=200, description="Gaps to return, at most; default 50.")


class _KnowledgeGap(BaseModel):
    # Stable id of the gap: the same question in the same namespace keeps it.
    gap_id: str
    # The most asked form of the question, normalised (folded, punctuation made spaces).
    head: str
    # Up to five other normalised queries merged into this gap.
    samples: list[str] = Field(default_factory=list)
    # Distinct askers: the people the tokens stand for.
    askers: int
    # Times it was asked in the window.
    asks: int
    # The day it was last asked (midnight UTC): a day, so it cannot single out one ask.
    last_at: datetime


class KnowledgeGapsListOutput(BaseModel):
    # Most askers first, then most asks.
    gaps: list[_KnowledgeGap] = Field(default_factory=list)
    # When the worker built this list; null before its first build, and while capturing is false.
    built_at: datetime | None = None
    # Whether capture is on for this namespace (its consent, or the deployment's capture
    # level when it has not chosen), null while enabled is false; false records no miss
    # and shows no gap.
    capturing: bool | None = None
    # Whether this deployment records misses (LOOM_KNOWLEDGE_GAPS_ENABLED).
    enabled: bool = False
