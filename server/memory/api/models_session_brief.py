"""session.brief_* — the condensed, resumable state of a line of work.

`session.brief_save` revises a brief from the events a session produced
since its last save (the coding plugins send them at PreCompact and
SessionEnd, zero model tokens on the client) or from sections an agent wrote
itself. `session.brief_get` hands the brief to the session that carries the
work on; `session.brief_list` shows which briefs a namespace holds;
`session.brief_delete` removes one at its writer's request.

A brief is working state, not retrieval memory: it is never embedded, ranked
or returned by `memory.search`. It is namespace-scoped, so any agent that
reads the namespace — a teammate, Codex after Claude Code — can resume it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.memory.api.models_common import _Identity
from server.memory.domain.session_brief import (
    STORED_KEY_MAX,
    STORED_KEY_PATTERN,
    BriefEvent,
    BriefSections,
)
from server.operations.base import MCP_HIDDEN

MAX_EVENTS = 4000
_KEY_OR_EMPTY = r"^$|" + STORED_KEY_PATTERN

Trigger = Literal["precompact", "session_end", "stop", "manual", "model"]


class SessionBriefSaveInput(_Identity):
    """Revise (or start) one brief. Give `events` (what happened since the
    last save) or `sections` (a brief you wrote yourself), or both."""

    # The brief's address in the namespace. Empty = `auto/<project>/<branch>`
    # (every session on the same line of work revises the same brief), or
    # `session/<session_id>` when neither is known.
    brief_key: str = Field(default="", max_length=STORED_KEY_MAX, pattern=_KEY_OR_EMPTY)
    # Project the work belongs to; with `branch` it forms the automatic key.
    # Empty keeps the stored brief's project.
    project: str = Field(default="", max_length=120)
    # Repository the work is in, stored on the brief; empty keeps the stored value.
    repo: str = Field(default="", max_length=300)
    # Branch the work is on; with `project` it forms the automatic key.
    # Empty keeps the stored brief's branch.
    branch: str = Field(default="", max_length=200)
    # Commit the checkout was at when saving; empty keeps the stored value.
    head_sha: str = Field(default="", max_length=64)
    # What prompted the save: `manual` and `model` always save; the automatic `precompact`,
    # `session_end` and `stop` store nothing while the namespace's recording is off.
    trigger: Trigger = "manual"
    # What happened since the last save (at most 4000 events), condensed into the revision.
    events: list[BriefEvent] = Field(default_factory=list, max_length=MAX_EVENTS)
    # A brief the calling agent wrote from its own context: the filled
    # sections replace the stored ones (decisions / dead ends append).
    sections: BriefSections | None = None
    # Opaque client position the events end at (a transcript line index). A
    # save repeating the previous save's session and cursor is a retry and
    # writes nothing.
    event_cursor: str = Field(default="", max_length=200, json_schema_extra=MCP_HIDDEN)
    # Compare-and-set: refuse (409) unless the stored revision is this one.
    if_revision: int | None = Field(default=None, ge=0, json_schema_extra=MCP_HIDDEN)
    # Force the deterministic builder (False) or the model (True); None =
    # the deployment setting.
    use_llm: bool | None = Field(default=None, json_schema_extra=MCP_HIDDEN)


class SessionBriefStats(BaseModel):
    events_in: int = 0
    windows: int = 0
    events_total: int = 0
    source_chars: int = 0
    brief_chars: int = 0
    # source_chars / brief_chars over the brief's whole life.
    compression_ratio: float = 0.0
    dropped_ungrounded: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_model: str = ""


class SessionBriefSaveOutput(BaseModel):
    # The key the brief was saved under (the resolved automatic key when none was given).
    brief_key: str
    # The brief's revision after this call (the stored one when unchanged; 0 when none is live).
    revision: int
    # True when a new revision was written; False when `reason` says why not.
    changed: bool
    # "llm" (revised by the model), "deterministic" (no model configured, or
    # it failed), "model" (the calling agent wrote the sections).
    author: str = ""
    # Why nothing was written: `nothing-to-save`, `recording_off` or `already-saved`;
    # empty when `changed`.
    reason: str = ""
    # The brief as text, ready to show; the stored one when nothing changed.
    rendered: str = ""
    # Counts for this save: events, windows, sizes, tokens and the model used.
    stats: SessionBriefStats = Field(default_factory=SessionBriefStats)


class SessionBriefGetInput(_Identity):
    """Load a brief: by `brief_key`, else the newest brief a given session
    wrote (`from_session`), else the newest for `project`."""

    # The brief to load; takes precedence over `from_session` and `project`.
    # A namespace-wide token may pass a `~<user>~…` key as session.brief_list shows it.
    brief_key: str = Field(default="", max_length=STORED_KEY_MAX, pattern=_KEY_OR_EMPTY)
    # Load the newest brief this session id contributed to.
    from_session: str = Field(default="", max_length=200)
    # Load the newest brief of this project (used when `brief_key` and
    # `from_session` are empty).
    project: str = Field(default="", max_length=120)
    # 0 = the current revision; N = that revision from the history.
    revision: int = Field(default=0, ge=0)
    # Also return the structured `sections`; False returns only `rendered`.
    include_sections: bool = Field(default=False, json_schema_extra=MCP_HIDDEN)
    # Append the client's own summary from its last
    # compaction, verbatim, below the brief (up to ~15k chars more).
    full: bool = False


class SessionBriefGetOutput(BaseModel):
    # False when no brief matched, the brief is deleted, or the revision is not in its history.
    found: bool
    # Key of the brief that matched.
    brief_key: str = ""
    # The revision returned (the current one unless `revision` was asked for).
    revision: int = 0
    # The brief as text, with the client's compaction summary appended when `full`.
    rendered: str = ""
    # The structured brief; None unless `include_sections` was set.
    sections: BriefSections | None = None
    # Project stored on the brief.
    project: str = ""
    # Branch stored on the brief.
    branch: str = ""
    # Commit the checkout was at when this revision was saved.
    head_sha: str = ""
    # Who wrote the revision: `llm`, `deterministic` or `model` (the calling agent).
    author: str = ""
    # What prompted the revision's save (`precompact`, `session_end`, `stop`,
    # `manual` or `model`).
    trigger: str = ""
    # The principal that saved the revision (`human:<subject>` / `process:<subject>`):
    # a namespace-wide writer may save to a user's own `~<user>~` brief.
    actor: str = ""
    # Session ids that contributed to the brief, oldest first (the last 20).
    source_sessions: list[str] = Field(default_factory=list)
    # When the returned revision was saved.
    updated_at: datetime | None = None
    # Seconds since `updated_at`.
    age_seconds: int = 0
    # How many revisions the brief's history holds.
    revisions: int = 0
    # Events condensed into the brief over its whole life.
    events_total: int = 0
    # Characters of event text condensed into the brief over its whole life.
    source_chars: int = 0
    # Length of `rendered` as stored, in characters.
    brief_chars: int = 0
    # True when the brief was deleted by its writer; nothing else is served then.
    deleted: bool = False


class SessionBriefListInput(_Identity):
    # Only briefs of this project; empty = every project.
    project: str = Field(default="", max_length=120)
    # True = only the calling agent's briefs; default every agent's.
    mine_only: bool = False
    # Only briefs this session id contributed to; empty = no session filter.
    from_session: str = Field(default="", max_length=200, json_schema_extra=MCP_HIDDEN)
    # At most this many briefs (1-50).
    limit: int = Field(default=10, ge=1, le=50)


class SessionBriefSummary(BaseModel):
    brief_key: str
    revision: int
    project: str = ""
    branch: str = ""
    agent: str = ""
    goal: str = ""
    status: str = ""
    author: str = ""
    # The principal that saved the revision, as on session.brief_get.
    actor: str = ""
    updated_at: datetime | None = None
    age_seconds: int = 0
    brief_chars: int = 0


class SessionBriefListOutput(BaseModel):
    # Live briefs, most recently saved first; empty when the namespace holds none.
    briefs: list[SessionBriefSummary] = Field(default_factory=list)
    # One line per brief, ready to show.
    rendered: str = ""


class SessionBriefDeleteInput(_Identity):
    # The brief to delete, as session.brief_list shows it.
    brief_key: str = Field(..., min_length=1, max_length=STORED_KEY_MAX, pattern=STORED_KEY_PATTERN)


class SessionBriefDeleteOutput(BaseModel):
    # False when no live brief has this key (unknown or already deleted).
    deleted: bool
    # Echo of the input `brief_key`.
    brief_key: str
    # The revision the brief was at when deleted; 0 when nothing was deleted.
    revision: int = 0
