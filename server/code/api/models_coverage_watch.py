"""`code.coverage.watch` / `code.coverage.unwatch` — a push instead of a poll.

A watch names a commit of a code namespace and one of the caller's task
inboxes. Once the fast tier and every compiler tier with a watermark cover
the commit (`code.index_status`'s `commit_coverage.ready`), one
`code.coverage.ready` event lands on the inbox: `/v1/task/watch`,
`task.updates` and the inbox's signed webhook carry it. The transport
authorizes viewer on the code namespace; the handler also requires writer on
the inbox's owner namespace.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from server.code.api.models_common import _CodeIdentity
from server.code.api.models_status import CodeCommitCoverage

_INBOX_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"


class CodeCoverageWatchInput(_CodeIdentity):
    # A full 40-hex SHA, or a 7..39-hex abbreviation the namespace's mirror resolves.
    commit: str = Field(min_length=7, max_length=40, description="Merge commit SHA to watch.")
    inbox: str = Field(
        min_length=1,
        max_length=128,
        pattern=_INBOX_PATTERN,
        description="Task inbox that receives the code.coverage.ready event; the caller "
        "must be a writer on its owner namespace.",
    )


class CodeCoverageWatchOutput(BaseModel):
    org: str
    namespace: str
    commit: str  # the full SHA
    # The inbox the event is posted to, as sent.
    inbox: str
    # True once every tier covers the commit; the event is then on the inbox.
    ready: bool = False
    # True when this call posted the event (an already-covered commit).
    emitted: bool = False
    # When the ready event was posted to the inbox; None while the commit is not covered.
    delivered_at: datetime | None = None
    # The event's task_id on the inbox, fixed per (namespace, commit, inbox).
    event_id: str
    # The commit's per-tier coverage at this call, as `code.index_status` reports it.
    commit_coverage: CodeCommitCoverage | None = None


class CodeCoverageUnwatchInput(_CodeIdentity):
    # The watched commit: a full 40-hex SHA, or a 7..39-hex abbreviation the mirror resolves.
    commit: str = Field(min_length=7, max_length=40)
    # The inbox the watch posts to; the caller must be a writer on its owner namespace.
    inbox: str = Field(min_length=1, max_length=128, pattern=_INBOX_PATTERN)


class CodeCoverageUnwatchOutput(BaseModel):
    # The full SHA the abbreviation resolved to.
    commit: str
    # The inbox, as sent.
    inbox: str
    # False when no live watch existed.
    removed: bool
