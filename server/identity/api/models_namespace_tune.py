"""Wire models for `namespace.tune`: drafts of the changes to the settings that create a
namespace's memories, each with its evidence, its real target and the write that would
apply it. Nothing is applied."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from server.identity.api.models_findings import FindingEvidence

# The settings a proposal can change. Extraction rules are drafted as the per-request
# instructions, the only place they live per writer: the operator extraction policy
# overrides every other instruction and turns off extraction's recovery passes, so it is
# never a draft's target.
TuneTarget = Literal[
    "extraction_instructions.request",
    "namespace.category",
    "namespace.guide",
]


class NamespaceTuneInput(BaseModel):
    """Draft changes to a namespace's extraction rules, categories and guide from what
    its rows and the audit show. Read-only: nothing is changed, and each draft names the
    write that would apply it for a person to approve."""

    org: str
    namespace: str
    # Ledger window the audit's usage checks cover, in seconds (default 7 days, 1 hour
    # to 90 days).
    window_seconds: int = Field(default=7 * 86400, ge=3600, le=90 * 86400)
    # Supporting rows cited per proposal, 1 to 20 (default 5).
    sample_limit: int = Field(default=5, ge=1, le=20)
    # A tag or key prefix on at least this many live rows is drafted: a tag with no
    # category of its name as a category, a prefix the guide does not name into the
    # guide (default 5).
    min_rows: int = Field(default=5, ge=2, le=10_000)
    # Category drafts returned, most used tags first, 0 to 20 (default 5).
    max_category_proposals: int = Field(default=5, ge=0, le=20)


class TuneApply(BaseModel):
    """The ordinary write that would apply a proposal, for a person to approve."""

    # The operation, e.g. `namespace.guide_set`.
    op: str
    # Its arguments as the proposal would send them.
    args: dict[str, Any]
    # False when `args` is a part to merge into each call the writer makes (the
    # instructions a memory.set_from_messages request adds to its own messages).
    args_complete: bool = True
    # The role the write needs.
    required_role: str
    # What keeps the write from overwriting a change made since the draft, or that
    # nothing does.
    guard: str


class TuneEvidence(BaseModel):
    """The rows a proposal rests on and the rows it must keep working for."""

    # The audit findings the proposal answers; empty for a category or guide draft, which
    # comes from the tags and the key layout.
    finding_codes: list[str] = Field(default_factory=list)
    # Rows that show the problem (the sum of the drafted findings' counts, so a row two
    # checks flag counts twice), or carry the tag the category would name; `supporting`
    # cites up to `sample_limit` of them, one from each finding in turn, or the newest
    # tagged rows.
    supporting_count: int = 0
    supporting: list[FindingEvidence] = Field(default_factory=list)
    # Rows the change must keep working for: at least this many rows of the smallest
    # population the drafted checks read that none of them flagged, or the tagged rows
    # another category already holds; None when that count could not be read, or for a
    # guide, which changes no stored row.
    counter_count: int | None = None


class TuneProposal(BaseModel):
    # Stable for the same target and proposed value, so a reviewer can refer to it.
    id: str
    target: TuneTarget
    # The scope the change takes effect in, e.g. "namespace acme/support".
    scope: str
    # One line on what the proposal changes.
    title: str
    # Why, from the evidence, and the expected effect.
    rationale: str
    # The setting's current value ("" when none is set or the caller may not read it).
    current: str
    # The revision `current` is at, when the setting is revisioned (the guide); else None.
    current_revision: int | None = None
    # The value the proposal would set.
    proposed: str
    evidence: TuneEvidence
    # The writers whose future memories the change shapes, most rows first, at most 20
    # (`affected_agents_total` counts all): for extraction instructions, the agents whose
    # rows extraction wrote; for a category or the guide, every agent that wrote live rows.
    affected_agents: list[str] = Field(default_factory=list)
    # None when the namespace's writers could not be read.
    affected_agents_total: int | None = 0
    # Always "future": a setting shapes the memories written after it changes.
    effect: Literal["future"] = "future"
    # What applying it does not do to rows already stored, and the separate action that
    # would.
    historical: str
    apply: TuneApply
    # What the proposal does not account for.
    limits: str = ""


class NamespaceTuneOutput(BaseModel):
    org: str
    namespace: str
    # When the drafts were read (UTC).
    computed_at: datetime
    # The drafts, extraction instructions first, then categories, then the guide.
    proposals: list[TuneProposal] = Field(default_factory=list)
    # The guide's current revision (0: none written), which a guide draft's
    # `expected_revision` carries; None when the guide could not be read.
    guide_revision: int | None = None
    # Categories registered in the namespace now; None when they could not be read.
    categories_registered: int | None = None
    # The operator extraction policy the namespace's extractions run under (the org's,
    # then the namespace's), for an org admin; None otherwise. A rule whose exact text
    # (case included) it contains is not drafted again.
    extraction_policy: str | None = None
    # Why `extraction_policy` is None, or "".
    extraction_policy_unavailable: str = ""
    # Sections that drafted nothing, as "section: reason": a read that failed, or evidence
    # that rules the draft out. A skipped section is not the same as nothing to draft.
    skipped: list[str] = Field(default_factory=list)
    # Proposals use no model: every draft is built from the rows, the audit and the
    # current settings.
    model_used: bool = False
