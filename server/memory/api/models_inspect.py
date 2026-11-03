"""Wire models for `memory.inspect`: what one user, agent or session holds in a namespace,
kept apart from what it can read there, with the settings that create its memories."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from server.core.naming import SENTINEL

InspectSubject = Literal["user", "agent", "session"]


class MemoryInspectInput(BaseModel):
    """Inspect one user, agent or session of a namespace, read-only.

    `subject` names which identity column `id` is matched against (`user_id`, `agent`,
    `session_id`). The subject is a filter inside the namespace the caller may already
    read, never a wider grant. Nothing is written: no recall credit, no usage event.
    """

    org: str
    namespace: str
    # Which identity column `id` names: "user" (user_id), "agent" or "session" (session_id).
    subject: InspectSubject
    # The user_id, agent name or session_id to inspect; the unset sentinel "-" and "*"
    # name no one and are refused.
    id: str = Field(min_length=1, max_length=256)
    # Newest rows listed per view, 0 to 50 (default 10).
    sample_limit: int = Field(default=10, ge=0, le=50)
    # Ledger window for the activity counts, in seconds (default 7 days, 1 hour to 90 days).
    window_seconds: int = Field(default=7 * 86400, ge=3600, le=90 * 86400)

    @field_validator("id")
    @classmethod
    def _names_someone(cls, v: str) -> str:
        v = v.strip()
        if v in ("", SENTINEL, "*"):
            raise ValueError("id must name a user, agent or session, not the unset sentinel")
        return v


class InspectCount(BaseModel):
    # The value counted: a memory type, status, source class, tag or category name.
    name: str
    # Live rows with that value.
    rows: int


class InspectRow(BaseModel):
    """One live row, with the identifiers a citation needs and a short excerpt."""

    memory_key: str
    memory_id: str
    version: int
    content_sha256: str
    memory_type: str
    status: str
    source_class: str
    agent: str
    user_id: str
    session_id: str
    # The principal the server recorded as the writer (`human:<subject>`,
    # `process:<subject>`); empty on rows written before it was recorded.
    actor: str
    updated_at: datetime
    # Versions the history table holds for this row, the entity tagger's re-emits
    # included (memory.history leaves those out).
    versions: int = 0
    # The value's first 200 characters; empty for a high-sensitivity row, readable by
    # key only.
    excerpt: str = ""


class InspectView(BaseModel):
    """One set of live rows of the namespace, described the same way."""

    # Which rows this view holds, in one sentence.
    definition: str
    # Live rows in the view.
    live_rows: int = 0
    # The view's rows by memory type, by status and by source class, most first.
    by_type: list[InspectCount] = Field(default_factory=list)
    by_status: list[InspectCount] = Field(default_factory=list)
    by_source_class: list[InspectCount] = Field(default_factory=list)
    # The most used tags, at most 20.
    top_tags: list[InspectCount] = Field(default_factory=list)
    # Registered categories (namespace.list_categories) the view's rows are filed under.
    categories: list[InspectCount] = Field(default_factory=list)
    # The oldest and the newest `updated_at` of the view's live rows (a row's last
    # write, not its first).
    first_updated_at: datetime | None = None
    last_updated_at: datetime | None = None
    # The newest rows, at most `sample_limit`.
    newest: list[InspectRow] = Field(default_factory=list)
    # True when `newest` lists every row of the view and the view's counts were read.
    complete: bool = True


class InspectCall(BaseModel):
    # The operation called, e.g. `memory.search`.
    operation: str
    # Calls of the operation filed under the subject in the window.
    calls: int
    # The ones that succeeded.
    succeeded: int


class InspectActivity(BaseModel):
    """The calls the ledger files under the subject in the window.

    A call is filed under the identity its arguments name, or the user and session a
    narrowed token is pinned to, whoever made it. The counts
    say what was attempted and what succeeded, never whether a result reached a model
    or changed an answer.
    """

    # The window the counts cover, in seconds.
    window_seconds: int
    # One entry per operation, most called first, at most 200.
    calls: list[InspectCall] = Field(default_factory=list)
    # The first call counted.
    first_at: datetime | None = None
    # The last call counted.
    last_at: datetime | None = None
    # Why the counts may cover less than the window (ledger retention), or "".
    gap: str = ""


class InspectSettings(BaseModel):
    """What creates the subject's memories, as far as the caller may read it."""

    # The namespace guide's current revision (0: none written) and summary.
    guide_revision: int = 0
    guide_summary: str = ""
    # Categories registered in the namespace.
    categories_registered: int = 0
    # The operator extraction policy extractions in this namespace run under (the org's
    # rules, then the namespace's), when the caller may read it; None otherwise.
    extraction_policy: str | None = None
    # Why `extraction_policy` is None, or "".
    extraction_policy_unavailable: str = ""
    # Where per-request extraction instructions come from: no stored per-user, per-agent or
    # per-session setting exists, so they are whatever each writer sends.
    extraction_instructions: str = (
        "Per request only: memory.set_from_messages and memory.bulk_set take "
        "`extraction_instructions`; no user, agent or session setting stores them."
    )


class MemoryInspectOutput(BaseModel):
    org: str
    namespace: str
    # The identity column inspected, as the request named it.
    subject: InspectSubject
    # The user_id, agent name or session_id inspected.
    id: str
    # When the report was read (UTC).
    computed_at: datetime
    # Rows whose identity names the subject.
    own: InspectView
    # Every other live row of the namespace: what the subject reads as a member but did
    # not write or is not filed under. Neither view counts promoted rows (loom.shared).
    others: InspectView
    # The calls the ledger files under the subject in the window.
    activity: InspectActivity
    # The guide, categories and extraction policy that create the subject's memories.
    settings: InspectSettings
    # Sections that could not be read, as "section: reason". A skipped section is not
    # an empty one.
    skipped: list[str] = Field(default_factory=list)
