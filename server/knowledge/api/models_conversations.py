"""Conversations plane — append-only message-level chat history.

Loom is not the live chat UI; this plane is the queryable record other
surfaces (Claude Desktop, Cursor, custom agents) append to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from server.knowledge.domain.conversations import ConversationRole
from server.operations.read_projection import ProjectedRow


class ConversationCreateInput(BaseModel):
    """Open a thread container. Optional — append creates the thread on
    first write either way; create just attaches title/metadata/run_id up front.
    """

    org: str
    namespace: str
    agent: str
    # The caller's id for the thread; re-creating the same id replaces its title,
    # metadata and `run_id`.
    thread_id: str
    # Display title shown in thread lists; empty = untitled.
    title: str = ""
    # Free-form JSON stored on the thread row; `conversation.get` returns it.
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_id: str = "-"
    user_id: str = "-"


class ConversationCreateOutput(BaseModel):
    # Echo of the input `thread_id`.
    thread_id: str
    # When this call wrote the thread row.
    created_at: datetime


class ConversationAppendInput(BaseModel):
    org: str
    namespace: str
    agent: str
    # The thread to append to; it need not have been created first.
    thread_id: str
    msg_id: str = ""  # optional; server derives a content hash when omitted
    # Who spoke: `user`, `assistant`, `system` or `tool`.
    role: ConversationRole
    # The message text, stored verbatim; a user or assistant turn is also embedded for
    # `conversation.search`.
    content: str
    # The model's reasoning for this turn, stored beside `content`; empty = none.
    reasoning: str = ""
    user_id: str = "-"
    run_id: str = "-"
    # Free-form JSON stored on the message; read back by `get` and `list`.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Token count the caller reports for the message, stored as given; 0 = not reported.
    tokens: int = Field(default=0, ge=0)


class ConversationAppendOutput(BaseModel):
    # The message id: the one sent, or the content hash the server derived.
    msg_id: str
    # Server time of this call; the message's timestamp when it was written.
    ts: datetime
    # Whether this call left a row in the transcript. False only when the
    # namespace's logging switch is off: the call is accepted (a client
    # cannot override the user's choice) but nothing is kept, and a caller
    # that plans around the transcript needs to know that.
    stored: bool = True
    # "" (written), "duplicate" (this msg_id already landed; stored=True),
    # "logging_disabled" (stored=False).
    reason: Literal["", "duplicate", "logging_disabled"] = ""


class ConversationSetLoggingInput(BaseModel):
    """Toggle whether this namespace records conversation transcripts. When
    disabled, conversation.append accepts calls but stores nothing."""

    org: str
    namespace: str
    # True records transcripts in this namespace; False makes `append` store nothing.
    enabled: bool


class ConversationSetLoggingOutput(BaseModel):
    namespace: str
    # The logging switch as it now stands.
    enabled: bool


class _ConversationEntry(BaseModel):
    msg_id: str
    # The agent that wrote this turn; a thread read with agent "*" holds several.
    # Empty from a server of the previous release, which does not send it.
    agent: str = ""
    role: ConversationRole
    content: str
    reasoning: str
    user_id: str
    run_id: str
    metadata: dict[str, Any]
    tokens: int
    ts: datetime


class ConversationUpdateInput(BaseModel):
    """Update one thread's title/metadata/run_id. Identity
    (org, namespace, agent, thread_id) is the key and can't be changed.
    """

    org: str
    namespace: str
    agent: str
    # The thread to update; an unknown thread returns `updated=False`.
    thread_id: str
    # Handler raises 400 if all three are absent (no-op signals a caller bug).
    title: str | None = None
    # New metadata JSON, replacing the old object whole; None leaves it unchanged.
    metadata: dict[str, Any] | None = None
    run_id: str | None = None


class ConversationUpdateOutput(BaseModel):
    # False when the thread has no thread row (never created); nothing is written then.
    updated: bool
    # Echo of the input `thread_id`.
    thread_id: str
    # The new `updated_at` of the thread; None when nothing was updated.
    updated_at: datetime | None = None


class ConversationDeleteInput(BaseModel):
    """Soft-delete one thread: sets `tombstoned_at` on messages + thread row +
    embeddings; read paths filter tombstoned rows, retention TTL hard-deletes.

    `confirm_thread_id` must equal `thread_id` — guards against UI-dropdown
    accidents. Single-thread scope; for user/namespace-wide erasure use
    `erasure.request` with `scope_type` `user_id` or `namespace`.
    """

    org: str
    namespace: str
    agent: str
    # The thread to soft-delete.
    thread_id: str
    # Must equal `thread_id`, or the call fails with 400 and nothing is deleted.
    confirm_thread_id: str = Field(..., min_length=1, max_length=256)


class ConversationDeleteOutput(BaseModel):
    # The tombstone time stamped on every affected row.
    tombstoned_at: datetime
    # Echo of the input `thread_id`.
    thread_id: str
    # {table_name: rows tombstoned}; for loom.conversation_embeddings, turns (one per
    # turn however many embedding models hold it).
    rows_affected: dict[str, int]
    # Sum of `rows_affected` across tables.
    total: int


class ConversationGetInput(BaseModel):
    """Fetch one thread by id: thread metadata + optional messages."""

    org: str
    namespace: str
    # "*" or "-" reads every agent's turns in the thread (a turn two agents both logged
    # once), for a caller that has only its id, as conversation.search reads "*" and "-";
    # any other value reads that one agent's.
    agent: str
    # The thread to fetch.
    thread_id: str
    # Default off: keeps the metadata lookup to two point reads. Flip on to replay.
    include_messages: bool = False
    # At most this many messages when `include_messages` is set (1-1000).
    limit: int = Field(default=100, ge=1, le=1000)
    # Messages to skip from the end `order` counts from.
    offset: int = Field(default=0, ge=0)
    # Which end of the thread the page is counted from. `asc` (default) is
    # oldest-first replay; `desc` is the tail: offset 0 / limit N returns the
    # newest N turns, newest first — one call for "the last N turns" instead
    # of fetching the whole thread and slicing client-side.
    order: Literal["asc", "desc"] = "asc"


class ConversationGetOutput(BaseModel):
    # False when the thread has neither a thread row nor any live message.
    found: bool
    # Echo of the input `thread_id`.
    thread_id: str
    # Title from `conversation.create` or `update`; empty when none was set.
    title: str = ""
    # Thread metadata JSON; `{}` when none was set.
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_id: str = "-"
    user_id: str = "-"
    # The thread's owner (the agent of its thread row); for a thread built by appends
    # only, the latest writer among the messages read, so empty without `include_messages`.
    agent: str = ""
    # When the thread row was first written; None for a thread built by appends only.
    created_at: datetime | None = None
    # When the thread row last changed; None for a thread built by appends only.
    updated_at: datetime | None = None
    # Live (not tombstoned) messages in the thread, returned even without messages.
    msg_count: int = 0
    # Populated only when include_messages=True; simple limit/offset slicing
    # (cursor-paginated walks belong on conversation.list).
    messages: list[_ConversationEntry] = Field(default_factory=list)
    # With `order="asc"`, the `conversation.list` cursor for the turns after this page;
    # None when the page reaches the end, or the order is `desc`.
    next_cursor: str | None = None


class ConversationListInput(BaseModel):
    org: str
    namespace: str
    # "*" or "-" reads every agent's turns in the thread (a turn two agents both logged
    # once), for a caller that has only its id, as conversation.search reads "*" and "-";
    # any other value reads that one agent's.
    agent: str
    # The thread to page through, oldest message first.
    thread_id: str
    run_id: str | None = None  # optional filter
    # At most this many messages per page (1-1000).
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None  # opaque (ts, msg_id)


class ConversationListOutput(BaseModel):
    # The page of messages in chronological order.
    rows: list[_ConversationEntry]
    # Pass as `cursor` for the next page; None when this is the last page. Read with "*",
    # two agents' different turns that share a millisecond and an id across a page
    # boundary lose the second to the cursor; the next release's cursor carries the text.
    next_cursor: str | None = None


class ConversationSearchInput(BaseModel):
    """Rank conversation messages by embedding similarity to `query`.

    `thread_id`, `run_id` and `role` each narrow the hits, and they combine. User and
    assistant turns and compaction summaries are embedded; when the embedder is
    unavailable a substring match stands in, newest first, and it also matches the
    other system turns and tool turns."""

    org: str
    namespace: str
    # "*" or "-" searches every agent's turns; any other value, that one agent's.
    agent: str
    # Text matched against message content, by embedding similarity, or by substring
    # when the embedder is unavailable.
    query: str
    thread_id: str | None = None  # optional: scope to one thread
    run_id: str | None = None  # optional: filter by run_id
    # Filter by speaker; system turns other than compaction summaries, and tool turns,
    # are not embedded, so they match only on the substring fallback.
    role: ConversationRole | None = None
    # At most this many hits (1-200). Across agents ("*" or "-") a turn several agents
    # logged is one hit: the embeddings hold one row per turn, and the substring path
    # collapses its copies before the limit.
    top_k: int = Field(default=20, ge=1, le=200)
    # "all_mine" also reads the caller's own namespaces, up to
    # `memory_search_fanout_max_namespaces`, as knowledge.search does.
    namespace_scope: Literal["this", "all_mine"] = "all_mine"


class _ConversationHit(BaseModel):
    thread_id: str
    msg_id: str
    role: ConversationRole
    content_excerpt: str  # truncated to 240 chars
    ts: datetime
    namespace: str = ""  # populated on cross-namespace search


class ConversationSearchOutput(BaseModel):
    # Matching messages, most similar first and empty when none clears the similarity
    # floor; newest first, with no floor, on the substring fallback.
    hits: list[_ConversationHit]
    # Namespaces of the caller an all_mine search did not read because it stops
    # at `memory_search_fanout_max_namespaces`; address one by name to search it.
    namespaces_skipped: list[str] = Field(default_factory=list)


# conversation.threads — recent thread list (grouped over loom.conversations),
# for the Sessions view's no-query "what threads exist" entry point.
class ConversationThreadsInput(BaseModel):
    org: str
    namespace: str
    agent: str = "*"  # "*" or "-" lists every agent's threads
    # Only threads with a live message in the last this many days (1-365).
    window_days: int = Field(default=30, ge=1, le=365)
    # At most this many threads (1-200).
    limit: int = Field(default=30, ge=1, le=200)
    # The output fields the caller reads (a whitelist
    # resolved against the row model; an unknown name is a 422); unset, every
    # field. Every list operation is projectable.
    fields: list[str] | None = None


class ConversationThreadSummary(ProjectedRow):
    thread_id: str
    agent: str
    msg_count: int
    first_seen: datetime
    last_seen: datetime
    last_role: str
    last_content_excerpt: str
    title: str = ""  # from conversation.create, if set; "" otherwise


class ConversationThreadsOutput(BaseModel):
    # Threads, most recently active first, each with its count and last message excerpt.
    threads: list[ConversationThreadSummary]


class ConversationCompactInput(BaseModel):
    """Summarize old turns into one embedded system turn and tombstone the originals.

    The turns are the oldest ones before the last `keep_recent` that fit the
    summarizer's prompt; their embeddings are tombstoned too, except a turn another
    agent of the thread still holds.
    The summary carries metadata.compacted_from=[msg_ids]; when its embed fails,
    nothing is written (`reason="embed-failed"`).

    Idempotent: re-running on an already-summarized window is a no-op.
    """

    org: str
    namespace: str
    agent: str
    # The thread to compact.
    thread_id: str
    # The newest this many messages stay verbatim; older ones are summarized, as many
    # as fit the summarizer's prompt.
    keep_recent: int = Field(default=10, ge=1, le=200)
    # Skip when fewer new turns than this precede the last keep_recent; an earlier
    # summary does not count.
    min_to_compact: int = Field(default=4, ge=1, le=50)


class ConversationCompactOutput(BaseModel):
    # True when a summary was written and the originals tombstoned; `reason` says why not.
    compacted: bool
    # How many older messages the summary replaced, an earlier summary it folded in not
    # counted; 0 when not compacted.
    messages_summarized: int
    # Id of the synthetic `system` summary message; empty when not compacted.
    summary_msg_id: str = ""
    # Prompt tokens the summarizer LLM call used; 0 when no call was made.
    tokens_in: int = 0
    # Completion tokens of the summary; 0 when no call was made.
    tokens_out: int = 0
    # Why compacted=False: nothing-to-compact, below-min-to-compact, already-compacted,
    # summarizer-empty, or embed-failed (retry once the embedder answers).
    reason: str = ""
