"""mentor.brief — the store's advice to one agent, with the evidence.

The mentor service (`server/insights/app/mentor.py`) reads what one agent did in
one namespace over a window and evaluates a fixed rule table naming the
plane it is not using. `context.pack` renders three of its hints at
session start and both transports attach one per call; this op returns the
whole profile — usage by op, the planes available to the namespace with a
one-line "when", every hint with the counts that fired it, and how often
this agent's past hints were followed — for the agent that wants the
reasoning, or the owner reading it from the CLI or the dashboard.

mentor.guide returns the agent guide the server ships (`skills/loom/`): how
to use Loom, for an agent that has no plugin to carry it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class MentorBriefInput(BaseModel):
    """One agent's usage profile and the store's hints. `agent` defaults
    to the caller's; `window_seconds` bounds the ledger read (7 days)."""

    org: str
    namespace: str
    agent: str = ""
    # How far back the ledger read reaches, in seconds; 1 hour to 90 days.
    window_seconds: int = Field(default=7 * 86400, ge=3600, le=90 * 86400)


class MentorHint(BaseModel):
    code: str
    severity: str
    text: str
    ops: list[str] = Field(default_factory=list)
    # The counts the rule fired on — checkable, never a value.
    evidence: dict[str, Any] = Field(default_factory=dict)
    # The plane of the first of the hint's ops that has one (a `planes` name), null when none
    # does.
    plane: str | None = None


class MentorPlane(BaseModel):
    plane: str
    calls: int = 0
    # The plane exists for this namespace (indexed repos, a tracker source,
    # documents, records…) — None when the mentor could not tell.
    available: bool | None = None
    when: str = ""
    # The mentor has a rule for this plane; the transcript, the profile, the
    # namespace ops and erasure are listed for completeness, never as "unused".
    mentored: bool = True


class MentorAdoption(BaseModel):
    # Sessions in the window where a hint of this code reached this agent,
    # and how many of those went on to call one of the hinted ops.
    code: str
    shown_sessions: int = 0
    followed_sessions: int = 0


UsageRungName = Literal[
    "attempt",
    "success",
    "committed_write",
    "indexed_write",
    "returned_hit",
    "client_acknowledged",
    "task_outcome",
]
UsageSource = Literal["server", "client"]


class UsageRung(BaseModel):
    """One rung of what the agent's use of Loom shows.

    A call made is not a call that answered, and an answer returned is not an answer
    used."""

    rung: UsageRungName
    # How many in the window; None when the read failed.
    count: int | None = None
    # Who recorded it: `server` (Loom saw it happen) or `client` (a caller reported it,
    # unverified).
    source: UsageSource
    # What the count is, and what it does not show.
    meaning: str


class MentorBriefOutput(BaseModel):
    org: str
    namespace: str
    agent: str
    # The window the profile was computed over, in seconds; a profile served from the cache
    # keeps the window it was computed with.
    window_seconds: int
    # When the profile was computed (UTC); older than the call when served from the cache.
    computed_at: datetime
    # Distinct client-named sessions of this agent in the window (`-` and `*` not counted).
    sessions: int
    # Calls per operation by this agent in the window.
    usage: dict[str, int] = Field(default_factory=dict)
    # Every plane the mentor knows, with this agent's calls to it and whether it is available.
    planes: list[MentorPlane] = Field(default_factory=list)
    # The rules that fired for this agent, each with the counts that fired it; empty when none.
    hints: list[MentorHint] = Field(default_factory=list)
    # Per hint code, sessions it was shown in and followed in, most shown first; empty when
    # no hint reached the agent or the read failed.
    adoption: list[MentorAdoption] = Field(default_factory=list)
    # The agent's use of Loom in the window, one rung each, weakest claim first: from a call
    # made to a graded run. Empty for no agent in particular; a rung whose read failed has no
    # count.
    usage_evidence: list[UsageRung] = Field(default_factory=list)
    # Oldest ledger row of this agent in the window, of any kind; None when it holds none or
    # the read failed.
    ledger_from: datetime | None = None
    # Days the configured retention keeps this namespace's call ledger rows; 0 keeps them
    # until deleted, None when the read failed.
    ledger_retention_days: int | None = None
    # The namespace's writers in the window, most active first; the brief
    # defaults to the first when the caller has no agent name of its own
    # (the dashboard) — `agent_defaulted` says so.
    agents: list[str] = Field(default_factory=list)
    # True when the caller named no real agent and the brief picked the top writer in `agents`.
    agent_defaulted: bool = False
    # Facts the brief could not read, as "step: reason".
    skipped: list[str] = Field(default_factory=list)
    # No warning or critical hint.
    healthy: bool = True


class MentorGuideInput(BaseModel):
    """The agent guide this server ships; the same for every caller."""

    org: str = ""
    # Which part: `skill` (the loop and rules, the default), `reference` (each plane) or `all`.
    part: Literal["skill", "reference", "all"] = "skill"


class MentorGuideOutput(BaseModel):
    # The guide's version (`metadata.version` of skills/loom/SKILL.md).
    version: str
    # The part returned, as requested.
    part: Literal["skill", "reference", "all"]
    # The guide as Markdown, without the skill's frontmatter.
    text: str
