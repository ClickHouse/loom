"""Wire models for a namespace's guide: `namespace.guide_get`, `guide_set` and
`guide_history`. Handlers are in server/identity/app/namespace_guide.py."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class NamespaceGuideGetInput(BaseModel):
    org: str
    namespace: str
    # A past revision to read; 0 (the default) reads the current guide.
    revision: int = Field(default=0, ge=0, le=2**32 - 1)


class NamespaceGuideGetOutput(BaseModel):
    # False when the revision read does not exist or cleared the guide.
    found: bool
    # The revision read, the current one being what `namespace.guide_set` takes as
    # `expected_revision`; 0 when that revision (or any guide) does not exist.
    revision: int = 0
    # The guide in Markdown: what the namespace holds, its key layout, what its tags mean.
    content: str = ""
    # One line for directories (`namespace.browse`) and the session-start pack.
    summary: str = ""
    # The writer's note on what this revision changed.
    note: str = ""
    # The actor (`human:<subject>` or `process:<subject>`) that wrote the revision.
    updated_by: str = ""
    # When the revision was written (UTC); None when the namespace never had a guide.
    updated_at: datetime | None = None


# The longest guide a write takes.
GUIDE_MAX_CHARS = 32_000  # not a setting: the wire limit both the write and a draft follow


class NamespaceGuideSetInput(BaseModel):
    org: str
    namespace: str
    # The whole new guide in Markdown, at most 32,000 characters; "" clears it.
    content: str = Field(max_length=GUIDE_MAX_CHARS)
    # One line, at most 300 characters, that a directory shows for the namespace;
    # left out, the current summary is kept, and "" removes it.
    summary: str | None = Field(default=None, max_length=300)
    # The revision this edit was made from (0 when there is no guide yet); a guide that
    # has moved past it refuses the write and answers with its current text.
    expected_revision: int = Field(ge=0, le=2**32 - 1)
    # What this revision changes and why, at most 500 characters.
    note: str = Field(default="", max_length=500)


class NamespaceGuideSetOutput(BaseModel):
    # True when this call wrote a new revision.
    written: bool
    # True when the guide moved past `expected_revision` and this save's text differs
    # from it: nothing was written, and `current` holds the guide to merge into.
    conflict: bool = False
    # The current revision after the call (the one written, or the one that refused
    # it), which the next edit passes as `expected_revision`.
    revision: int
    # On a conflict, the current guide; otherwise None.
    current: NamespaceGuideGetOutput | None = None


class NamespaceGuideHistoryInput(BaseModel):
    org: str
    namespace: str
    # Newest revisions to list, at most 200.
    limit: int = Field(default=20, ge=1, le=200)


class _GuideRevision(BaseModel):
    # The revision's number, counted from 1.
    revision: int
    # Its one-line summary; empty when it had none or cleared the guide.
    summary: str
    # The writer's note on what it changed.
    note: str
    # The actor (`human:<subject>` or `process:<subject>`) that wrote it.
    updated_by: str
    # When it was written (UTC).
    updated_at: datetime
    # Length of the revision's content in characters; 0 for a revision that cleared it.
    chars: int


class NamespaceGuideHistoryOutput(BaseModel):
    # Newest first; read one whole with `namespace.guide_get` and its `revision`.
    revisions: list[_GuideRevision]
