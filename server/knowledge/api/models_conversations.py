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
    # `conversation.search`, unless its text reads as high.
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
    # False when recording is off or the turn was removed while embedding ran.
    stored: bool = True
    # "" (written), "duplicate" (this msg_id already landed; stored=True),
    # "logging_disabled" or "deleted" (stored=False).
    reason: Literal["", "duplicate", "logging_disabled", "deleted"] = ""


class ConversationSaveMessageInput(BaseModel):
    """Save one chat message a user picked for search, whether or not the
    namespace records transcripts.

    `conversation.append` records a transcript and stores nothing while the
    namespace's logging switch (`conversation.set_logging`) is off; this op is
    one user's opt-in for one message and never changes that switch. The
    message is flagged saved for its owner, and
    `conversation.search_saved_messages` searches only flagged messages. A
    message the transcript already holds is flagged in place (its run_id and
    content stay as recorded), not written twice.
    """

    org: str
    namespace: str
    agent: str
    # The client's conversation id; this remains the thread id in Loom.
    conversation_id: str = Field(min_length=1, max_length=256)
    # The client's stable message id. Re-saving it is idempotent.
    message_id: str = Field(min_length=1, max_length=256)
    # Only selectable user and assistant messages are embedded.
    role: Literal["user", "assistant"]
    # The message text; ignored when the transcript already holds the message.
    content: str = Field(min_length=1, max_length=120_000)
    # The owner is a hard filter on the paired search operation.
    user_id: str = Field(min_length=1, max_length=256)


class ConversationSaveMessageOutput(BaseModel):
    conversation_id: str
    message_id: str
    # True when the turn is saved and has a vector the saved search ranks: a new
    # write, a re-save (finishing what an interrupted save left behind), and a
    # transcript turn that is now flagged saved. False with a `reason` otherwise,
    # never for a disabled logging switch.
    indexed: bool
    # "duplicate": already saved, nothing changed. "owner_mismatch": the stored
    # turn belongs to another user. "deleted": the stored turn was tombstoned.
    # "unsupported_role": the stored turn is a system or tool turn, which is not
    # embedded. "embed_failed": the turn is saved but its embed failed; saving it
    # again retries the embed. "high_sensitivity": the turn's text reads as high
    # (a credential, card number, SSN or stated PIN), which is never embedded, so it is
    # not indexed.
    reason: Literal[
        "",
        "duplicate",
        "owner_mismatch",
        "deleted",
        "unsupported_role",
        "embed_failed",
        "high_sensitivity",
    ] = ""
    # The stored turn's timestamp; the time of the refusal when one is refused.
    ts: datetime


class ConversationUnsaveMessageInput(BaseModel):
    """Withdraw one message from a user's saved-message search."""

    org: str
    namespace: str
    agent: str
    # The client's conversation id, as given to conversation.save_message.
    conversation_id: str = Field(min_length=1, max_length=256)
    # The client's message id, as given to conversation.save_message.
    message_id: str = Field(min_length=1, max_length=256)
    # Only this user's saved row is withdrawn.
    user_id: str = Field(min_length=1, max_length=256)


class ConversationUnsaveMessageOutput(BaseModel):
    conversation_id: str
    message_id: str
    # False when no saved row of this user exists: never saved, already
    # withdrawn, or saved by another user (which is not confirmed).
    found: bool
    # True when the turn itself was removed: the namespace does not record
    # transcripts, so the row existed only because of the save. False keeps
    # the turn in the recorded transcript, no longer flagged saved.
    removed: bool = False
    ts: datetime


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
    # The thread's new run; None or "-" leaves it unchanged, "" detaches it from its run.
    run_id: str | None = None


class ConversationUpdateOutput(BaseModel):
    # False when the thread has no thread row (never created); nothing is written then.
    updated: bool
    # Echo of the input `thread_id`.
    thread_id: str
    # The new `updated_at` of the thread; None when nothing was updated.
    updated_at: datetime | None = None


class ConversationDeleteInput(BaseModel):
    """Soft-delete one thread: its messages, thread row and embeddings no other agent holds.

    Sets `tombstoned_at`: read paths filter tombstoned rows, and the retention TTL
    on `loom.conversations`, when set, hard-deletes the messages. A turn another
    agent of the thread holds live with the same text, as a turn search indexes (user,
    assistant or a summary), keeps its embedding, which the two copies share.

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
    # Thread metadata JSON; `{}` when none was set, apart from the titler's
    # `title_refused` mark.
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
    run_id: str | None = None  # optional filter; None or "-" = every run
    # At most this many messages per page (1-1000).
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None  # opaque (ts, msg_id)


class ConversationListOutput(BaseModel):
    # The page of messages in chronological order.
    rows: list[_ConversationEntry]
    # Pass as `cursor` for the next page; None when this is the last page. Read with "*",
    # two agents' different turns that share a millisecond and an id across a page
    # boundary lose the second to the cursor, which carries only (ts, msg_id).
    next_cursor: str | None = None


class ConversationSearchInput(BaseModel):
    """Rank conversation messages by embedding similarity to `query`.

    `thread_id`, `run_id` and `role` each narrow the hits, and they combine. User and
    assistant turns and compaction summaries are embedded, unless their text reads as high;
    when the embedder is unavailable a substring match stands in, newest first, and it also
    matches the other system turns and tool turns. No path returns text that reads as high:
    the vector path checks the text its embedding was built from, and the substring
    fallback checks the whole turn."""

    org: str
    namespace: str
    # "*" or "-" searches every agent's turns; any other value, that one agent's.
    agent: str
    # Text matched against message content, by embedding similarity, or by substring
    # when the embedder is unavailable.
    query: str
    thread_id: str | None = None  # optional: scope to one thread
    run_id: str | None = None  # optional: filter by run_id; None or "-" = every run
    # Filter by speaker; system turns other than compaction summaries, and tool turns,
    # are not embedded, so they match only on the substring fallback.
    role: ConversationRole | None = None
    # Optional owner filter; None or "-" = every user. Integrations that serve many
    # users from one namespace set it so a search cannot cross their user boundary.
    user_id: str | None = None
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


class ConversationSearchSavedMessagesInput(BaseModel):
    """Hybrid search over only the messages one user saved with
    conversation.save_message, not the whole transcript.

    `conversation.search` ranks every recorded turn of a namespace (or of all
    the caller's namespaces); this op stays in one namespace and returns only
    turns flagged saved and owned by `user_id`.
    """

    org: str
    namespace: str
    agent: str
    user_id: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1, max_length=20_000)
    conversation_id: str | None = Field(default=None, max_length=256)
    role: Literal["user", "assistant"] | None = None
    top_k: int = Field(default=20, ge=1, le=200)
    # hybrid: the vector leg and a BM25 leg over each message's whole text,
    # fused by reciprocal rank; semantic: vector only; lexical: BM25 only.
    search_mode: Literal["hybrid", "semantic", "lexical"] = "hybrid"
    # Weight of the BM25 leg in the fusion (hybrid mode); 1.0 = equal weight.
    lexical_weight: float = Field(default=1.0, ge=0.0, le=1.0)


class _SavedMessageHit(ProjectedRow):
    conversation_id: str
    message_id: str
    role: Literal["user", "assistant"]
    # A window of the message: its opening, or the text around the first matched term
    # in a keyword hit; empty in the saved list when it reads as high.
    content_excerpt: str
    ts: datetime


class ConversationSearchSavedMessagesOutput(BaseModel):
    hits: list[_SavedMessageHit]


class ConversationListSavedMessagesInput(BaseModel):
    """Page through the messages a user saved, newest first, with no query: the
    first view of a "Saved" panel before anything is typed."""

    org: str
    namespace: str
    agent: str
    # Only this user's saved messages are listed.
    user_id: str = Field(min_length=1, max_length=256)
    # Optional: only the messages saved from one conversation.
    conversation_id: str | None = Field(default=None, max_length=256)
    # At most this many messages per page (1-200).
    limit: int = Field(default=50, ge=1, le=200)
    # Opaque; pass the previous page's `next_cursor`.
    cursor: str | None = None
    # The output fields the caller reads (a whitelist resolved against the row model;
    # an unknown name is a 422); unset, every field.
    fields: list[str] | None = None


class ConversationSavedStatusInput(BaseModel):
    """Which of a conversation's messages a user has saved, in one call: a chat
    client draws every message's saved state when it opens the conversation."""

    org: str
    namespace: str
    agent: str
    user_id: str = Field(min_length=1, max_length=256)
    # The conversation the messages belong to; message ids are unique within it.
    conversation_id: str = Field(min_length=1, max_length=256)
    # The messages to check (1-500).
    message_ids: list[str] = Field(min_length=1, max_length=500)


class ConversationSavedStatusOutput(BaseModel):
    # The subset of `message_ids` this user has saved, in the order asked.
    saved: list[str]


class ConversationListSavedMessagesOutput(BaseModel):
    # The page, most recently saved first.
    rows: list[_SavedMessageHit]
    # Pass as `cursor` for the next page; None when this is the last page.
    next_cursor: str | None = None


# conversation.threads — recent thread list (grouped over loom.conversations),
# for the Conversations page's no-query "what threads exist" entry point.
class ConversationThreadsInput(BaseModel):
    org: str
    namespace: str
    agent: str = "*"  # "*" or "-" lists every agent's threads
    # Only threads with a live message in the last this many days (1-365).
    window_days: int = Field(default=30, ge=1, le=365)
    # At most this many threads (1-200).
    limit: int = Field(default=30, ge=1, le=200)
    # Threads to skip before the page (0-100,000); default 0, the first page.
    offset: int = Field(default=0, ge=0, le=100_000)
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
    # The last turn's first 240 characters; empty when it reads as high.
    last_content_excerpt: str
    # Empty when untitled or when the title reads as high.
    title: str = ""


class ConversationThreadsOutput(BaseModel):
    # Threads, most recently active first, each with its count and last message excerpt.
    threads: list[ConversationThreadSummary]


class ConversationCompactInput(BaseModel):
    """Summarize old turns into one embedded system turn and tombstone the originals.

    The turns are the oldest ones before the last `keep_recent` that fit the
    summarizer's prompt; their embeddings are tombstoned too, except a turn another
    agent of the thread still holds live with the same text, as a turn search indexes
    (user, assistant or a summary).
    The summary carries metadata.compacted_from=[msg_ids]; when its embed fails,
    no summary is written and no turn is tombstoned (`reason="embed-failed"`), and the
    same holds for a summary that reads as high (`reason="summary-high-sensitivity"`).

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
    # How many older messages the summary replaced, not counting an earlier summary; 0
    # when not compacted.
    messages_summarized: int
    # Id of the synthetic `system` summary message; empty when not compacted.
    summary_msg_id: str = ""
    # Prompt tokens the summarizer LLM call used; 0 when no call was made.
    tokens_in: int = 0
    # Completion tokens of the summary; 0 when no call was made.
    tokens_out: int = 0
    # Why compacted=False: nothing-to-compact, below-min-to-compact, already-compacted,
    # summarizer-empty, embed-failed (retry once the embedder answers),
    # summary-high-sensitivity (do not retry: the same window gets the same summary).
    reason: str = ""
