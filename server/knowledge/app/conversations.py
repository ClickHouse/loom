"""conversation.{create,append,update,delete,get,list,search,...} handlers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from server.auth.fanout import namespaces_to_search
from server.core.cursors import decode_cursor as _decode_cursor
from server.core.cursors import encode_cursor as _encode_cursor
from server.core.datetimes import naive_utc_now
from server.core.errors import AuthInsufficientError
from server.core.naming import SENTINEL, named
from server.embedding import get_embedder
from server.identity.app.conversation_logging import set_conversation_logging
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import ReadFilters
from server.knowledge.api.models_conversations import (
    ConversationAppendInput,
    ConversationAppendOutput,
    ConversationCompactInput,
    ConversationCompactOutput,
    ConversationCreateInput,
    ConversationCreateOutput,
    ConversationDeleteInput,
    ConversationDeleteOutput,
    ConversationGetInput,
    ConversationGetOutput,
    ConversationListInput,
    ConversationListOutput,
    ConversationListSavedMessagesInput,
    ConversationListSavedMessagesOutput,
    ConversationSavedStatusInput,
    ConversationSavedStatusOutput,
    ConversationSaveMessageInput,
    ConversationSaveMessageOutput,
    ConversationSearchInput,
    ConversationSearchOutput,
    ConversationSearchSavedMessagesInput,
    ConversationSearchSavedMessagesOutput,
    ConversationSetLoggingInput,
    ConversationSetLoggingOutput,
    ConversationThreadsInput,
    ConversationThreadsOutput,
    ConversationThreadSummary,
    ConversationUnsaveMessageInput,
    ConversationUnsaveMessageOutput,
    ConversationUpdateInput,
    ConversationUpdateOutput,
    _ConversationEntry,
    _ConversationHit,
    _SavedMessageHit,
)
from server.knowledge.app.conversation_search import (
    ConversationHit,
    ConversationVectorQuery,
    vector_search,
)
from server.knowledge.domain.conversations import (
    EMBEDDED_EXCERPT_CHARS,
    LISTED_MARGIN_CHARS,
    THREAD_EXCERPT_CHARS,
    metadata_object,
)
from server.memory.domain.sensitivity import (
    HELD_MARGIN,
    listed_text,
    reads_high,
    without_high,
)
from server.observability.budget_shape import note_budget_shape
from server.observability.genai import staged
from server.observability.otel import traced
from server.operations.read_projection import project_rows
from server.settings.budgets import llm_max_tokens

if TYPE_CHECKING:
    from server.knowledge.store.conversations import (
        ConversationEmbeddingRow,
        MessageInsert,
        StoredMessage,
    )

log = logging.getLogger(__name__)

# Why conversation.save_message did not index a turn (`indexed=False`).
SaveRefusal = Literal[
    "owner_mismatch", "deleted", "unsupported_role", "embed_failed", "high_sensitivity"
]


async def namespace_recording_enabled(org: str, namespace: str) -> bool:
    """The namespace owner's Record conversations switch. Fails closed: a
    namespace with no stored answer is not recording."""
    from server.knowledge.store import conversations as conversation_repo

    return await conversation_repo.namespace_logging_enabled(org, namespace)


async def handle_conversation_create(
    input: ConversationCreateInput,
) -> ConversationCreateOutput:
    """Open a thread container. Idempotent: re-creating an existing
    (org, namespace, thread_id) tuple replaces title/metadata/run_id
    (ReplacingMergeTree(updated_at))."""
    from server.knowledge.store import conversations as conversation_repo

    now = naive_utc_now()
    await conversation_repo.insert_thread(
        {
            "org": input.org,
            "namespace": input.namespace,
            "agent": input.agent,
            "thread_id": input.thread_id,
            "title": input.title,
            "metadata": json.dumps(input.metadata, sort_keys=True),
            "run_id": input.run_id,
            "user_id": input.user_id,
            "created_at": now,
            "updated_at": now,
        }
    )
    return ConversationCreateOutput(thread_id=input.thread_id, created_at=now)


async def handle_conversation_append(
    input: ConversationAppendInput,
) -> ConversationAppendOutput:
    """Insert one message row, then its embedding row once the message landed.

    A failed embed still stores the turn; its embedding row records the error. A turn
    that reads as high is stored with no embedding row, so its text never
    reaches the embedder and conversation.search never ranks it.
    """
    from server.knowledge.store import conversations as conversation_repo

    now = naive_utc_now()

    # msg_id is optional: a chat client self-logging via the MCP primer passes
    # only role + content, so derive a deterministic content hash when it's
    # omitted — repeats of the same turn then dedupe under the
    # ReplacingMergeTree instead of doubling.
    msg_id = (
        input.msg_id
        or hashlib.sha256(f"{input.thread_id}|{input.role}|{input.content}".encode()).hexdigest()[
            :32
        ]
    )

    # The namespace's Record conversations switch. When off, accept the call
    # but store nothing — a client can't override the user's choice.
    with traced("ch.query.conversation_logging_enabled"):
        logging_on = await conversation_repo.namespace_logging_enabled(input.org, input.namespace)
    if not logging_on:
        return ConversationAppendOutput(
            msg_id=msg_id, ts=now, stored=False, reason="logging_disabled"
        )

    # Idempotency: loom.conversations is append-only (plain MergeTree, no engine
    # dedup), so skip the write when this (thread_id, msg_id) already landed —
    # a re-logged turn (a model self-logging via the primer, a gateway retry)
    # must not double-write the transcript.
    with traced("ch.query.conversation_message_exists"):
        already = await conversation_repo.message_exists(
            input.org, input.namespace, input.agent, input.thread_id, msg_id
        )
    if already:
        return ConversationAppendOutput(msg_id=msg_id, ts=now, reason="duplicate")

    msg_row = conversation_repo.MessageInsert(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        thread_id=input.thread_id,
        msg_id=msg_id,
        role=input.role,
        content=input.content,
        reasoning=input.reasoning,
        user_id=input.user_id,
        run_id=input.run_id,
        metadata=json.dumps(input.metadata, sort_keys=True),
        tokens=input.tokens,
        ts=now,
    )
    # Only user/assistant turns are embedded for recall. System/tool messages
    # stay in the transcript (loom.conversations) but are NOT indexed — they're
    # scaffolding (tool output, system reminders) that pollutes
    # conversation.search ranking and wastes embed spend. A turn that reads as
    # high is stored but never embedded, so conversation.search never ranks it.
    # A turn can be megabytes long, so the check runs off the event loop.
    if input.role not in ("user", "assistant") or await asyncio.to_thread(
        reads_high, input.content
    ):
        with traced("ch.insert.conversation", role=input.role):
            await conversation_repo.append(msg_row)
        return ConversationAppendOutput(msg_id=msg_id, ts=now)

    async def _insert_message() -> None:
        with traced("ch.insert.conversation", role=input.role):
            await conversation_repo.append(msg_row)

    # The message insert and the embed are independent, so they run concurrently: run
    # sequentially the ~66 ms insert would sit in front of a ~210 ms embed. The embedding
    # row is written only once the message row landed, so a failed insert leaves no
    # searchable turn; both complete before the response, so conversation.search keeps
    # its read-your-write guarantee.
    _, embedded = await asyncio.gather(
        _insert_message(),
        _embed_turn(
            input.content,
            org=input.org,
            namespace=input.namespace,
            thread_id=input.thread_id,
            msg_id=msg_id,
        ),
    )
    async with conversation_repo.saved_message_write_lock(
        input.org, input.namespace, input.thread_id
    ):
        with traced("ch.query.conversation_message_exists"):
            live = await conversation_repo.message_exists(
                input.org, input.namespace, input.agent, input.thread_id, msg_id, live_only=True
            )
        if not live:
            await conversation_repo.tombstone_embedding_if_message_gone(
                input.org, input.namespace, input.agent, input.thread_id, msg_id
            )
            return ConversationAppendOutput(msg_id=msg_id, ts=now, stored=False, reason="deleted")
        await _store_embedding(msg_row, embedded)
        # A save can finish while embedding runs. Copy its saved state onto
        # this embedding, which may replace the save's version under FINAL.
        with traced("ch.query.conversation_saved_owner"):
            owner = await conversation_repo.saved_message_owner(
                input.org, input.namespace, input.agent, input.thread_id, msg_id
            )
        if owner is not None:
            await _mark_saved(
                _TurnKey(input.org, input.namespace, input.agent, input.thread_id, msg_id),
                owner,
                tables=("loom.conversation_embeddings",),
            )
    return ConversationAppendOutput(msg_id=msg_id, ts=now)


class EmbeddedTurn(NamedTuple):
    """One turn's embed: the text embedded, its vector, the model that built it, and
    the error when it failed (no vector, `dim` 0)."""

    excerpt: str
    vector: list[float]
    dim: int
    model: str
    error: str


async def _embed_turn(
    content: str, *, org: str, namespace: str, thread_id: str, msg_id: str
) -> EmbeddedTurn:
    """Embed a turn's first `EMBEDDED_EXCERPT_CHARS` characters. Fail-open: a failed
    embed returns no vector and the error. The caller checks `reads_high` first: a
    turn that reads as high is never embedded."""
    embedder = get_embedder()
    excerpt = content[:EMBEDDED_EXCERPT_CHARS]
    try:
        with traced(
            "embed.compute.conversation", model=embedder.model_name, excerpt_len=len(excerpt)
        ):
            vector = await embedder.embed(excerpt)
        return EmbeddedTurn(excerpt, vector, embedder.dim, embedder.model_name, "")
    except Exception as e:  # noqa: BLE001 — a failed embed is returned to the caller, not raised
        log.warning(
            "conversation embed failed (org=%s ns=%s thread=%s msg=%s): %s",
            org,
            namespace,
            thread_id,
            msg_id,
            e,
        )
        return EmbeddedTurn(excerpt, [], 0, embedder.model_name, f"{type(e).__name__}: {e}")


async def _store_embedding(msg: MessageInsert, embedded: EmbeddedTurn) -> None:
    """Store the embedding row of message `msg` from `_embed_turn`'s result, with the
    message's identity, user, saved flag and `ts`, then re-check the message
    (`tombstone_embedding_if_message_gone`). A failed embed is stored too, with the
    dead-letter columns recording the error."""
    assert msg.ts is not None, "an embedded message carries its own ts"
    from server.knowledge.store import conversations as conversation_repo

    embedding_row: ConversationEmbeddingRow = {
        "org": msg.org,
        "namespace": msg.namespace,
        "agent": msg.agent,
        "thread_id": msg.thread_id,
        "msg_id": msg.msg_id,
        "role": msg.role,
        # The text the vector was built from, which a re-embed starts from again;
        # search hits show its first conversation_search_excerpt_chars characters.
        "content_excerpt": embedded.excerpt,
        "embedding_model": embedded.model,
        "embedding_dim": embedded.dim,
        "embedding": embedded.vector,
        "user_id": msg.user_id,
        "run_id": msg.run_id,
        "embed_attempts": 1 if embedded.dim > 0 else 0,
        "embed_last_error": embedded.error,
        "embed_failed_at": naive_utc_now() if embedded.error else None,
        "is_saved": msg.is_saved,
        "saved_at": msg.saved_at,
        "ts": msg.ts,
    }
    with traced("ch.insert.conversation_embedding", embedded=embedded.dim > 0):
        await conversation_repo.insert_embedding(embedding_row)
    await conversation_repo.tombstone_embedding_if_message_gone(
        msg.org, msg.namespace, msg.agent, msg.thread_id, msg.msg_id
    )


@dataclass(frozen=True)
class _TurnKey:
    org: str
    namespace: str
    agent: str
    thread_id: str
    msg_id: str


async def _mark_saved(key: _TurnKey, user_id: str, *, tables: tuple[str, ...]) -> None:
    from server.knowledge.store import conversations as conversation_repo

    for table in tables:
        with traced("ch.mutate.conversation_mark_saved", table=table):
            await conversation_repo.mark_message_saved(
                table,
                key.org,
                key.namespace,
                key.agent,
                key.thread_id,
                key.msg_id,
                user_id=user_id,
            )


async def handle_conversation_save_message(
    input: ConversationSaveMessageInput,
) -> ConversationSaveMessageOutput:
    """Persist one user's explicit search opt-in without enabling transcript capture."""
    from server.knowledge.store import conversations as conversation_repo

    async with conversation_repo.saved_message_write_lock(
        input.org, input.namespace, input.conversation_id
    ):
        return await _save_message(input)


async def _save_message(input: ConversationSaveMessageInput) -> ConversationSaveMessageOutput:
    from server.knowledge.store import conversations as conversation_repo

    key = _TurnKey(input.org, input.namespace, input.agent, input.conversation_id, input.message_id)
    user_id = input.user_id
    changed = False
    embedded: EmbeddedTurn | None = None
    stored = await _stored_turn(key, user_id)
    if stored is None:
        if await asyncio.to_thread(reads_high, input.content):
            return _save_refused(input, "high_sensitivity")  # nothing written
        # The vector is needed either way, so embed while the row is written.
        _, embedded = await asyncio.gather(
            _write_saved_turn(input, once_per_identity=True),
            _embed_turn(
                input.content,
                org=key.org,
                namespace=key.namespace,
                thread_id=key.thread_id,
                msg_id=key.msg_id,
            ),
        )
        stored = await _stored_turn(key, user_id)
        if stored is None:
            # The deduplication window still holds a block of this turn whose rows
            # were since erased, and dropped this one.
            note_budget_shape("rewrite")
            await _write_saved_turn(input, once_per_identity=False)
            stored = await _stored_turn(key, user_id)
        if stored is None:
            raise RuntimeError("conversation turn vanished between its write and its re-read")
        changed = True

    refusal = await asyncio.to_thread(_save_refusal, stored, user_id)
    if refusal is not None:
        return _save_refused(input, refusal)
    if stored.unsaved:
        await _mark_saved(key, user_id, tables=("loom.conversations",))
        stored = await _stored_turn(key, user_id)
        if stored is None:
            raise RuntimeError("conversation turn vanished while it was flagged saved")
        # Another user's save claimed the turn first.
        refusal = await asyncio.to_thread(_save_refusal, stored, user_id)
        if refusal is not None:
            return _save_refused(input, refusal)
        changed = True

    with traced("ch.query.conversation_embedding_state"):
        emb = await conversation_repo.embedding_state(
            key.org,
            key.namespace,
            key.agent,
            key.thread_id,
            key.msg_id,
            user_id=user_id,
            embedding_model=get_embedder().model_name,
        )
    if emb.foreign:
        return _save_refused(input, "owner_mismatch")
    if emb.current == 0:
        # A save interrupted between its two writes, a failed embed's placeholder, or
        # a transcript turn whose embedding never landed: embed the stored text.
        if embedded is None or embedded.excerpt != stored.content[:EMBEDDED_EXCERPT_CHARS]:
            embedded = await _embed_turn(
                stored.content,
                org=key.org,
                namespace=key.namespace,
                thread_id=key.thread_id,
                msg_id=key.msg_id,
            )
        await _store_embedding(
            conversation_repo.MessageInsert(
                org=key.org,
                namespace=key.namespace,
                agent=key.agent,
                thread_id=key.thread_id,
                msg_id=key.msg_id,
                role=stored.role,
                content=stored.content,
                user_id=user_id,
                run_id=stored.run_id,
                is_saved=True,
                saved_at=stored.saved_at,
                ts=max(stored.ts, emb.version_ts or stored.ts),
            ),
            embedded,
        )
        if embedded.error:
            return _save_refused(input, "embed_failed")
        changed = True
    elif emb.unsaved:
        await _mark_saved(key, user_id, tables=("loom.conversation_embeddings",))
        changed = True
    return ConversationSaveMessageOutput(
        conversation_id=input.conversation_id,
        message_id=input.message_id,
        indexed=True,
        reason="" if changed else "duplicate",
        ts=stored.ts,
    )


async def _stored_turn(key: _TurnKey, user_id: str) -> StoredMessage | None:
    from server.knowledge.store import conversations as conversation_repo

    with traced("ch.query.conversation_message_state"):
        return await conversation_repo.message_state(
            key.org, key.namespace, key.agent, key.thread_id, key.msg_id, user_id=user_id
        )


async def _write_saved_turn(
    input: ConversationSaveMessageInput, *, once_per_identity: bool
) -> None:
    """The transcript row of a turn the transcript does not hold, flagged saved
    for its user. The namespace's logging switch does not apply: saving is the
    user's own choice for that one turn."""
    from server.knowledge.store import conversations as conversation_repo

    with traced("ch.insert.conversation", role=input.role):
        await conversation_repo.append(
            conversation_repo.MessageInsert(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                thread_id=input.conversation_id,
                msg_id=input.message_id,
                role=input.role,
                content=input.content,
                user_id=input.user_id,
                is_saved=True,
                saved_at=naive_utc_now(),
                ts=naive_utc_now(),
            ),
            once_per_identity=once_per_identity,
        )


def _save_refusal(stored: StoredMessage, user_id: str) -> SaveRefusal | None:
    """Why a stored turn must not be (re)claimed by `user_id`, or None. Checked
    on every save, already-saved turns included, before anything is changed."""
    if stored.tombstoned:
        return "deleted"
    # The stored role wins over the request's: only user and assistant turns are
    # embedded, so a system or tool turn cannot be found by the saved search.
    if stored.role not in ("user", "assistant"):
        return "unsupported_role"
    if any(owner not in ("-", "", user_id) for owner in stored.owners):
        return "owner_mismatch"
    # A turn that reads as high is never embedded, so the saved search could
    # never find it.
    if reads_high(stored.content):
        return "high_sensitivity"
    return None


def _save_refused(
    input: ConversationSaveMessageInput, reason: SaveRefusal
) -> ConversationSaveMessageOutput:
    return ConversationSaveMessageOutput(
        conversation_id=input.conversation_id,
        message_id=input.message_id,
        indexed=False,
        reason=reason,
        ts=naive_utc_now(),
    )


async def handle_conversation_unsave_message(
    input: ConversationUnsaveMessageInput,
) -> ConversationUnsaveMessageOutput:
    """Withdraw a message from its owner's saved-message search.

    A namespace that records transcripts keeps the turn, unflagged: the owner
    chose to record it. One that does not removes it, since the row existed
    only because of the save. A row saved by another user is reported as not
    found, never as someone else's.
    """
    from server.knowledge.store import conversations as conversation_repo

    _conversation_read_scope()
    async with conversation_repo.saved_message_write_lock(
        input.org, input.namespace, input.conversation_id
    ):
        return await _unsave_message(input)


async def _unsave_message(
    input: ConversationUnsaveMessageInput,
) -> ConversationUnsaveMessageOutput:
    from server.knowledge.store import conversations as conversation_repo

    key = _TurnKey(input.org, input.namespace, input.agent, input.conversation_id, input.message_id)
    now = naive_utc_now()
    with traced("ch.query.conversation_saved_owner"):
        owner = await conversation_repo.saved_message_owner(
            key.org, key.namespace, key.agent, key.thread_id, key.msg_id
        )
    if owner is None:
        owner = await conversation_repo.saved_message_owner(
            key.org,
            key.namespace,
            key.agent,
            key.thread_id,
            key.msg_id,
            table="loom.conversation_embeddings",
        )
    if owner != input.user_id:
        return ConversationUnsaveMessageOutput(
            conversation_id=input.conversation_id,
            message_id=input.message_id,
            found=False,
            ts=now,
        )
    with traced("ch.query.conversation_logging_enabled"):
        recording = await conversation_repo.namespace_logging_enabled(input.org, input.namespace)
    # Reconciliation needs the transcript to interpret older embedding excerpts.
    for table in reversed(conversation_repo.SAVED_MESSAGE_TABLES):
        with traced("ch.mutate.conversation_unsave", table=table):
            await conversation_repo.unsave_message(
                table,
                key.org,
                key.namespace,
                key.agent,
                key.thread_id,
                key.msg_id,
                user_id=input.user_id,
                remove=not recording,
                when=now,
            )
    return ConversationUnsaveMessageOutput(
        conversation_id=input.conversation_id,
        message_id=input.message_id,
        found=True,
        removed=not recording,
        ts=now,
    )


async def handle_conversation_set_logging(
    input: ConversationSetLoggingInput,
) -> ConversationSetLoggingOutput:
    """Toggle the per-namespace conversation-logging switch (the dashboard's Record
    conversations switch); conversation.append enforces it (no-op when off).
    The switch lives on the namespace directory row."""
    await set_conversation_logging(input.org, input.namespace, input.enabled)
    return ConversationSetLoggingOutput(namespace=input.namespace, enabled=input.enabled)


async def handle_conversation_update(
    input: ConversationUpdateInput,
) -> ConversationUpdateOutput:
    """Update one thread's title, metadata, and/or run_id.

    Merges new fields onto the live row and re-inserts with a fresh
    `updated_at`. Returns `updated=False` when the thread doesn't exist.
    Refuses no-op calls (all optional fields unset) with 400 rather than
    writing a no-change row.
    """
    from fastapi import HTTPException

    from server.knowledge.store import conversations as conversation_repo

    # None and the identity sentinel "-" the SDKs stamp keep the thread's run; "" clears it.
    if input.run_id == SENTINEL:
        input = input.model_copy(update={"run_id": None})
    if input.title is None and input.metadata is None and input.run_id is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_FIELDS_PROVIDED",
                "message": (
                    "conversation.update requires at least one of `title`, `metadata`, or "
                    '`run_id` (`run_id` "-" keeps the thread\'s run; "" detaches it).'
                ),
            },
        )

    existing = await conversation_repo.fetch_thread(
        input.org,
        input.namespace,
        input.agent,
        input.thread_id,
    )
    if existing is None:
        return ConversationUpdateOutput(updated=False, thread_id=input.thread_id)

    now = naive_utc_now()
    merged_title = input.title if input.title is not None else existing.get("title", "")
    merged_metadata = (
        json.dumps(input.metadata) if input.metadata is not None else existing.get("metadata", "{}")
    )
    if input.run_id is None:
        merged_run_id = existing.get("run_id", SENTINEL) or SENTINEL
    else:
        merged_run_id = input.run_id or SENTINEL

    await conversation_repo.insert_thread(
        {
            "org": input.org,
            "namespace": input.namespace,
            "agent": input.agent,
            "thread_id": input.thread_id,
            "title": merged_title,
            "metadata": merged_metadata,
            "run_id": merged_run_id,
            "user_id": existing.get("user_id", "-") or "-",
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "tombstoned_at": None,
        }
    )
    return ConversationUpdateOutput(
        updated=True,
        thread_id=input.thread_id,
        updated_at=now,
    )


async def handle_conversation_delete(
    input: ConversationDeleteInput,
) -> ConversationDeleteOutput:
    """Soft-delete one thread (messages, thread row, and embeddings no other agent holds).

    Tombstone, not hard-delete: rows disappear from reader queries
    immediately because the read paths filter `tombstoned_at IS NULL`; the
    retention TTL on `loom.conversations`, when set, hard-deletes the messages.
    For user/namespace-wide erasure use `erasure.request` with `scope_type`
    `user_id` or `namespace`.
    """
    from fastapi import HTTPException

    from server.knowledge.store import conversations as conversation_repo

    if input.confirm_thread_id != input.thread_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONFIRM_THREAD_ID_MISMATCH",
                "message": (
                    "`confirm_thread_id` must equal `thread_id`. "
                    "Mid-weight defense against destructive UI-dropdown "
                    "accidents."
                ),
            },
        )

    now = naive_utc_now()
    async with conversation_repo.saved_message_write_lock(
        input.org, input.namespace, input.thread_id
    ):
        counts = await conversation_repo.tombstone_thread(
            input.org,
            input.namespace,
            input.agent,
            input.thread_id,
            when=now,
        )
    return ConversationDeleteOutput(
        tombstoned_at=now,
        thread_id=input.thread_id,
        rows_affected=counts,
        total=sum(counts.values()),
    )


async def handle_conversation_get(
    input: ConversationGetInput,
) -> ConversationGetOutput:
    """Fetch one thread's metadata + optional message slice.

    The `msg_count` read is unconditional (cheap) so callers can decide
    whether to flip `include_messages` based on size.

    The reads run under `sequential_reads()`: a `get` that follows the caller's own
    `append` must see the turn, and the count and the slice must agree. Without it, on a
    multi-replica service the two statements can land on different replicas and report
    `msg_count=1` with zero messages until replication catches up (~4 s observed). One
    thread's rows are a point read, so the Keeper round-trip is cheap here.
    """
    from server.core.ch import sequential_reads

    with sequential_reads():
        return await _conversation_get_reads(input)


def _thread_reader(agent: str) -> str | None:
    """The agent filter for one thread's reads: "*" or "-" reads every agent's turns
    (a caller that has only the thread id: a search hit, a memory's source), as
    conversation.search reads them."""
    return None if agent in ("*", SENTINEL) else agent


async def _conversation_get_reads(input: ConversationGetInput) -> ConversationGetOutput:
    from server.knowledge.store import conversations as conversation_repo

    agent = _thread_reader(input.agent)
    thread = await conversation_repo.fetch_thread(
        input.org,
        input.namespace,
        agent,
        input.thread_id,
    )

    msg_count = await conversation_repo.count_thread_msgs(
        input.org,
        input.namespace,
        agent,
        input.thread_id,
    )

    # A thread exists if it carries a metadata row (conversation.create) OR
    # any messages. The thread LIST is derived from messages alone
    # (handle_conversation_threads), so an append-only thread — built without
    # a prior conversation.create — is real and must be openable. The
    # metadata row is enrichment (title/run_id); absent, those default empty.
    if thread is None and msg_count == 0:
        return ConversationGetOutput(found=False, thread_id=input.thread_id)
    thread_row: Mapping[str, Any] = thread if thread is not None else {}

    messages: list[_ConversationEntry] = []
    next_cursor: str | None = None
    writer = agent or ""
    if input.include_messages:
        # list_thread has no offset arg, so pull (offset + limit) and slice.
        # `order="desc"` counts the page from the newest turn, so offset 0
        # is the thread's tail and the messages come back newest first.
        # Oldest first, one row past the page says whether another follows.
        peek = 1 if input.order == "asc" else 0
        rows = await conversation_repo.list_thread(
            input.org,
            input.namespace,
            agent,
            input.thread_id,
            limit=input.offset + input.limit + peek,
            order=input.order,
        )
        page = rows[input.offset : input.offset + input.limit]
        if input.order == "asc" and page and len(rows) > input.offset + input.limit:
            next_cursor = _encode_cursor(page[-1].ts, page[-1].msg_id)
        if page and not writer:
            writer = max(page, key=lambda r: r.ts).agent
        messages = [
            _ConversationEntry(
                msg_id=r.msg_id,
                agent=r.agent,
                role=r.role,
                content=r.content,
                reasoning=r.reasoning,
                user_id=r.user_id,
                run_id=r.run_id,
                metadata=metadata_object(r.metadata),
                tokens=r.tokens,
                ts=r.ts,
            )
            for r in page
        ]

    return ConversationGetOutput(
        found=True,
        thread_id=input.thread_id,
        title=thread_row.get("title", "") or "",
        metadata=metadata_object(thread_row.get("metadata")),
        run_id=thread_row.get("run_id", "-") or "-",
        user_id=thread_row.get("user_id", "-") or "-",
        # The owner (the thread row's agent), else the latest writer read.
        agent=thread_row.get("agent", "") or writer,
        created_at=thread_row.get("created_at"),
        updated_at=thread_row.get("updated_at"),
        msg_count=msg_count,
        messages=messages,
        next_cursor=next_cursor,
    )


async def handle_conversation_list(
    input: ConversationListInput,
) -> ConversationListOutput:
    """Paginate one thread's history in chronological order."""
    from server.knowledge.store import conversations as conversation_repo

    after = _decode_cursor(input.cursor) if input.cursor else None
    rows = await conversation_repo.list_thread(
        input.org,
        input.namespace,
        _thread_reader(input.agent),
        input.thread_id,
        run_id=named(input.run_id),
        after=after,
        limit=input.limit + 1,  # +1 to detect "more"
    )

    truncated = len(rows) > input.limit
    page = rows[: input.limit]
    entries = [
        _ConversationEntry(
            msg_id=r.msg_id,
            agent=r.agent,
            role=r.role,
            content=r.content,
            reasoning=r.reasoning,
            user_id=r.user_id,
            run_id=r.run_id,
            metadata=metadata_object(r.metadata),
            tokens=r.tokens,
            ts=r.ts,
        )
        for r in page
    ]
    next_cursor = _encode_cursor(page[-1].ts, page[-1].msg_id) if page and truncated else None
    return ConversationListOutput(rows=entries, next_cursor=next_cursor)


def _conversation_read_scope() -> ReadFilters:
    """The request's read scope for conversation rows. They carry a user but no
    session, so a session-bounded token cannot be kept to its rows and is refused."""
    scope = request_scope()
    if scope.scope == "own_session":
        raise AuthInsufficientError(
            "conversation rows carry no session_id, so read_scope=own_session cannot "
            "bound them; call with an own_user or namespace-scoped token"
        )
    return scope


async def handle_conversation_search(
    input: ConversationSearchInput,
) -> ConversationSearchOutput:
    # The identity sentinel "-" the SDKs stamp filters nothing here. The saved-message
    # search shares `_search_conversations` and keeps its user_id exact: it is the owner.
    input = input.model_copy(
        update={"run_id": named(input.run_id), "user_id": named(input.user_id)}
    )
    return await _search_conversations(input, saved_only=False)


async def _search_conversations(
    input: ConversationSearchInput,
    *,
    saved_only: bool,
) -> ConversationSearchOutput:
    """Hybrid semantic + filter search.

    Embed the query, rank `loom.conversation_embeddings` by cosine
    distance with recency tiebreak. Falls back to substring search when
    the embedder is unavailable so the endpoint never 5xx's.
    """
    import asyncio

    from server.settings import settings

    embedder = get_embedder()
    max_distance = 1.0 - settings.conversation_search_min_similarity
    query_vec: list[float] | None
    try:
        query_vec = await embedder.embed(input.query[:EMBEDDED_EXCERPT_CHARS])
    except Exception as e:  # noqa: BLE001 — embed failure falls back to substring search
        log.warning("conversation embed query failed; falling back to substring: %s", e)
        query_vec = None

    scope = _conversation_read_scope()
    namespaces, skipped = await namespaces_to_search(
        input.org, input.namespace, input.namespace_scope
    )
    if query_vec is None:
        return await _search_substring_fallback(
            input, namespaces, skipped, scope=scope, saved_only=saved_only
        )

    async def _search_one(ns: str) -> tuple[str, list[ConversationHit]]:
        return ns, await vector_search(
            ConversationVectorQuery(
                org=input.org,
                namespace=ns,
                agent=input.agent,
                embedding_model=embedder.model_name,
                query_vec=query_vec,
                top_k=input.top_k,
                scope=scope,
                thread_id=input.thread_id or None,
                role=input.role or None,
                run_id=input.run_id,
                user_id=input.user_id,
                saved_only=saved_only,
                max_distance=max_distance,
            )
        )

    results_per_ns = await asyncio.gather(*[_search_one(ns) for ns in namespaces])
    # Carry similarity so the cross-namespace merge ranks by cosine, not
    # insertion order.
    scored: list[tuple[float, _ConversationHit]] = []
    for ns, results in results_per_ns:
        for r in results:
            similarity = 1.0 - r.distance
            scored.append(
                (
                    similarity,
                    _ConversationHit(
                        thread_id=r.thread_id,
                        msg_id=r.msg_id,
                        role=r.role,
                        content_excerpt=r.content_excerpt,
                        ts=r.ts,
                        namespace=ns,
                    ),
                )
            )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return ConversationSearchOutput(
        hits=[h for _, h in scored[: input.top_k]], namespaces_skipped=skipped
    )


async def _search_substring_fallback(
    input: ConversationSearchInput,
    namespaces: list[str],
    skipped: list[str],
    *,
    scope: ReadFilters,
    saved_only: bool,
) -> ConversationSearchOutput:
    """Lexical fallback used when the embedder is unavailable: the same
    namespaces, newest match first."""
    from server.knowledge.store import conversations as conversation_repo
    from server.settings import settings

    rows = await conversation_repo.search_substring(
        input.org,
        namespaces,
        input.agent,
        input.query,
        scope=scope,
        thread_id=input.thread_id or None,
        role=input.role or None,
        run_id=input.run_id,
        user_id=input.user_id,
        saved_only=saved_only,
        # Spare rows, so the turns held back below do not leave the page short.
        limit=input.top_k + HELD_MARGIN,
    )
    # The fallback checks each matched turn's whole text and drops one that reads as
    # high. A matched turn can be megabytes long, so the check runs off the event loop.
    rows = await asyncio.to_thread(without_high, rows, lambda r: r.content, input.top_k)
    hits = [
        _ConversationHit(
            thread_id=r.thread_id,
            msg_id=r.msg_id,
            role=r.role,
            content_excerpt=r.content[: settings.conversation_search_excerpt_chars],
            ts=r.ts,
            namespace=r.namespace,
        )
        for r in rows
    ]
    return ConversationSearchOutput(hits=hits, namespaces_skipped=skipped)


async def handle_conversation_search_saved_messages(
    input: ConversationSearchSavedMessagesInput,
) -> ConversationSearchSavedMessagesOutput:
    """Search only messages this user explicitly saved through save_message.

    The vector leg embeds each message's opening characters; the BM25 leg reads
    its whole text, so an exact term (an id, a table name) or a phrase deep in a
    long saved answer still ranks. `hybrid` fuses the two by reciprocal rank."""
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.knowledge.store import conversations as conversation_repo

    scope = _conversation_read_scope()
    # The internal fusion pool may exceed the 200-hit wire limit.
    candidate_limit = max(input.top_k * 3, 20) if input.search_mode == "hybrid" else input.top_k
    semantic: list[_SavedMessageHit] = []
    lexical: list[_SavedMessageHit] = []
    if input.search_mode != "lexical":
        result = await _search_conversations(
            ConversationSearchInput(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                query=input.query,
                thread_id=input.conversation_id,
                role=input.role,
                user_id=input.user_id,
                top_k=input.top_k,
                namespace_scope="this",
            ).model_copy(update={"top_k": candidate_limit}),
            saved_only=True,
        )
        semantic = [
            _SavedMessageHit(
                conversation_id=hit.thread_id,
                message_id=hit.msg_id,
                role=hit.role,
                content_excerpt=hit.content_excerpt,
                ts=hit.ts,
            )
            for hit in result.hits
            if hit.role in ("user", "assistant")
        ]
    if input.search_mode != "semantic":
        tokens = knowledge_bm25.tokenize(input.query)
        with traced("ch.query.conversation_saved_lexical", tokens=len(tokens)):
            rows = await conversation_repo.lexical_saved_messages(
                input.org,
                input.namespace,
                input.agent,
                tokens,
                scope=scope,
                user_id=input.user_id,
                conversation_id=input.conversation_id,
                role=input.role,
                # Spare rows, for the messages held back below.
                limit=candidate_limit + HELD_MARGIN,
            )
        # A message that reads as high is never a keyword hit either: one saved
        # by an earlier build is held back here, as the vector leg holds it back.
        # The check reads each message whole, so it runs in a thread, off the event loop.
        plain = await asyncio.to_thread(
            without_high,
            [r for r in rows if r["role"] in ("user", "assistant")],
            lambda r: str(r["content"]),
            candidate_limit,
        )
        lexical = [
            _SavedMessageHit(
                conversation_id=r["thread_id"],
                message_id=r["msg_id"],
                role=r["role"],
                content_excerpt=_snippet(r["content"], tokens),
                ts=r["saved_at"],
            )
            for r in plain
        ]
    if input.search_mode == "semantic":
        return ConversationSearchSavedMessagesOutput(hits=semantic)
    if input.search_mode == "lexical":
        return ConversationSearchSavedMessagesOutput(hits=lexical)
    return ConversationSearchSavedMessagesOutput(
        hits=_fuse_saved(semantic, lexical, top_k=input.top_k, w_lex=input.lexical_weight)
    )


# Reciprocal-rank constant, as knowledge.search fuses its legs.
_RRF_K = 60.0


def _fuse_saved(
    semantic: list[_SavedMessageHit],
    lexical: list[_SavedMessageHit],
    *,
    top_k: int,
    w_lex: float,
) -> list[_SavedMessageHit]:
    """score = 1/(k + rank_sem) + w_lex/(k + rank_lex) over the union, keyed by
    (conversation, message). A lexical hit's excerpt is kept: it is centred on
    the matched terms. Ties keep the semantic leg's order."""

    def key(h: _SavedMessageHit) -> tuple[str, str]:
        return (h.conversation_id, h.message_id)

    hits: dict[tuple[str, str], _SavedMessageHit] = {key(h): h for h in semantic}
    hits.update({key(h): h for h in lexical})
    score: dict[tuple[str, str], float] = dict.fromkeys(hits, 0.0)
    for rank, h in enumerate(semantic, start=1):
        score[key(h)] += 1.0 / (_RRF_K + rank)
    for rank, h in enumerate(lexical, start=1):
        score[key(h)] += w_lex / (_RRF_K + rank)
    order = {k: i for i, k in enumerate(hits)}
    ranked = sorted(hits, key=lambda k: (-score[k], order[k]))
    return [hits[k] for k in ranked[:top_k]]


def _snippet(content: str, tokens: list[str]) -> str:
    """An excerpt around the first query term the content contains, so a match
    deep in a long message shows the matching words; the opening otherwise."""
    from server.settings import settings

    size = settings.conversation_search_excerpt_chars
    lowered = content.lower()
    hits = [p for p in (lowered.find(t) for t in tokens) if p >= 0]
    if not hits or min(hits) < size // 2:
        return _excerpt(content, size)
    start = max(min(hits) - size // 3, 0)
    window = content[start : start + size]
    return "…" + window + ("…" if start + size < len(content) else "")


async def handle_conversation_list_saved_messages(
    input: ConversationListSavedMessagesInput,
) -> ConversationListSavedMessagesOutput:
    """List one user's saved messages, newest first, a page at a time."""
    from server.knowledge.store import conversations as conversation_repo
    from server.settings import settings

    # Read once, so the length read from ClickHouse and the cut agree.
    size = settings.conversation_search_excerpt_chars
    scope = _conversation_read_scope()
    before: tuple[datetime, str, str] | None = None
    if input.cursor:
        ts, ids = _decode_cursor(input.cursor)
        thread_id, _, msg_id = ids.partition("\x1f")
        before = (ts, thread_id, msg_id)
    with traced("ch.query.conversation_list_saved"):
        rows = await conversation_repo.list_saved_messages(
            input.org,
            input.namespace,
            input.agent,
            scope=scope,
            user_id=input.user_id,
            # The excerpt the listing shows and a margin past it: a key crossing the
            # cut reads as high in what is checked.
            listed=size + LISTED_MARGIN_CHARS,
            conversation_id=input.conversation_id,
            before=before,
            limit=input.limit + 1,  # +1 to detect "more"
        )
    page = rows[: input.limit]
    # Listed without the text when it reads as high, as memory.list lists a
    # 'high' row. Checking every text is CPU work; it runs off the event loop.
    listed = await asyncio.to_thread(lambda: [listed_text(r["content"]) for r in page])
    hits = [
        _SavedMessageHit(
            conversation_id=r["thread_id"],
            message_id=r["msg_id"],
            role=r["role"],
            content_excerpt=_excerpt(text, size),
            ts=r["saved_at"],
        )
        for r, text in zip(page, listed, strict=True)
        if r["role"] in ("user", "assistant")
    ]
    next_cursor = None
    if len(rows) > input.limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["saved_at"], f"{last['thread_id']}\x1f{last['msg_id']}")
    project_rows(
        hits, _SavedMessageHit, input.fields, always=frozenset({"conversation_id", "message_id"})
    )
    return ConversationListSavedMessagesOutput(rows=hits, next_cursor=next_cursor)


async def handle_conversation_saved_status(
    input: ConversationSavedStatusInput,
) -> ConversationSavedStatusOutput:
    """Report which of one conversation's messages this user has saved."""
    from server.knowledge.store import conversations as conversation_repo

    scope = _conversation_read_scope()
    with traced("ch.query.conversation_saved_status", asked=len(input.message_ids)):
        saved = await conversation_repo.saved_message_ids(
            input.org,
            input.namespace,
            input.agent,
            input.conversation_id,
            input.message_ids,
            scope=scope,
            user_id=input.user_id,
        )
    return ConversationSavedStatusOutput(
        saved=[m for m in dict.fromkeys(input.message_ids) if m in saved]
    )


def _excerpt(content: str, size: int) -> str:
    return content[:size] + "…" if len(content) > size else content


async def handle_conversation_threads(input: ConversationThreadsInput) -> ConversationThreadsOutput:
    """Recent thread list grouped over loom.conversations.

    Group by thread_id, aggregate message count + first/last seen,
    project the most recent message's role + content excerpt so the
    Conversations page has something to click without typing a query.
    """
    from server.knowledge.store import conversations as conversation_repo

    rows = await conversation_repo.recent_threads(
        input.org,
        input.namespace,
        window_days=int(input.window_days),
        limit=input.limit,
        offset=input.offset,
        agent=_thread_reader(input.agent) if input.agent else None,
        count_turns=not input.fields or "msg_count" in input.fields,
        last_turn=not input.fields
        or bool({"agent", "last_role", "last_content_excerpt"} & set(input.fields)),
    )
    # Titles live on the conversation_threads metadata table (set by
    # conversation.create).
    titles = await conversation_repo.thread_titles(
        input.org, input.namespace, [str(r["thread_id"]) for r in rows]
    )
    # Listed across the namespace: a last turn or a title that reads as high is
    # listed without its text, as memory.list lists a 'high' row. The excerpt and
    # LISTED_MARGIN_CHARS characters past it are checked, so a key that crosses the
    # excerpt's cut is caught too. Checking every text is CPU work; it runs off the
    # event loop.
    listed = await asyncio.to_thread(
        lambda: [
            (listed_text(str(r["last_content"])), listed_text(titles.get(str(r["thread_id"]), "")))
            for r in rows
        ]
    )
    threads = [
        ConversationThreadSummary(
            thread_id=str(r["thread_id"]),
            agent=str(r["last_agent"]),
            msg_count=int(r["msg_count"]),
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
            last_role=str(r["last_role"]),
            last_content_excerpt=last_content[:THREAD_EXCERPT_CHARS],
            title=title,
        )
        for r, (last_content, title) in zip(rows, listed, strict=True)
    ]
    project_rows(threads, ConversationThreadSummary, input.fields, always=frozenset({"thread_id"}))
    return ConversationThreadsOutput(threads=threads)


# The summarizer's prompt budget, in characters of `role: content` lines.
_COMPACT_TRANSCRIPT_CHARS = 16000
# What folded earlier summaries may add to it, all of them together: a summary is at
# most 600 tokens, and normally only the one the last run wrote is in the window.
_FOLDED_SUMMARY_CHARS = 4000


def _fitting_prefix(lines: list[str], cap: int) -> int:
    """How many leading lines fit in `cap` characters joined by newlines;
    at least one, so a single oversized message is still compacted (clipped)."""
    used = 0
    for i, line in enumerate(lines):
        used += len(line) + (1 if i else 0)
        if used > cap:
            return max(1, i)
    return len(lines)


def _compaction_window(
    lines: list[str], is_summary: list[bool], stale: list[bool]
) -> tuple[list[int], str]:
    """`(replaced, transcript)`: the indices of the leading candidate lines the summary
    replaces, and the prompt transcript. `stale` marks summaries to replace unread;
    `_window_of` keeps that true only while every source is replaced."""
    # Leading summaries fold in free while each fits what is left of the allowance
    # whole. One that does not stays live and out of the prompt (tombstoning it would
    # drop text the summarizer never read), and the window still reaches the first
    # new turn. A stale summary is replaced unread: its sources, replaced too, say all
    # it says.
    k, allowance, read, dropped = 0, _FOLDED_SUMMARY_CHARS, [], []
    while k < len(lines) and is_summary[k]:
        if stale[k]:
            dropped.append(k)
        elif len(lines[k]) <= allowance:
            allowance -= len(lines[k])
            read.append(k)
        k += 1
    fit = k + _fitting_prefix(lines[k:], _COMPACT_TRANSCRIPT_CHARS)
    turns = "\n".join(lines[k:fit])[:_COMPACT_TRANSCRIPT_CHARS]
    transcript = "\n".join(part for part in (*(lines[i] for i in read), turns) if part)
    return sorted([*read, *dropped, *range(k, fit)]), transcript


def _window_of(candidates: list[dict[str, Any]], is_summary: list[bool]) -> tuple[list[int], str]:
    """`_compaction_window` over the candidate rows. A summary is stale when a failed run
    could have left it: its sources are all among the candidates (so all live), and its
    `ts` is the earliest of theirs. It stays stale only while this window replaces every
    source; otherwise it is read like any other summary, so no source loses its cover."""
    lines = [f"{r['role']}: {r['content']}" for r in candidates]
    sources = [set(_compacted_from(r)) for r in candidates]
    ts_of = {str(r["msg_id"]): r["ts"] for r in candidates}
    stale = [
        bool(s) and s <= ts_of.keys() and r["ts"] == min(ts_of[i] for i in s)
        for r, s in zip(candidates, sources, strict=True)
    ]
    while True:
        replaced, transcript = _compaction_window(lines, is_summary, stale)
        covered = {str(candidates[i]["msg_id"]) for i in replaced}
        uncovered = [i for i in replaced if stale[i] and not sources[i] <= covered]
        if not uncovered:
            return replaced, transcript
        for i in uncovered:
            stale[i] = False


def _compacted_from(row: Mapping[str, Any]) -> list[str]:
    """The msg_ids a compaction summary replaced, from its metadata.compacted_from
    list; empty for any other turn. A summary is a system turn with a non-empty list,
    the same test `_PEER_COPIES` applies in SQL."""
    if row.get("role") != "system":
        return []
    ids = metadata_object(row.get("metadata")).get("compacted_from")
    return [str(i) for i in ids] if isinstance(ids, list) else []


def _compaction_candidates(
    turns: list[dict[str, Any]], min_to_compact: int
) -> tuple[list[dict[str, Any]], list[bool]]:
    """The turns before `keep_recent` that compaction may summarize, oldest first, and
    for each whether it is an earlier summary.

    A saved turn is the user's explicit choice to keep that message findable: it stays
    verbatim, never folded into a summary and tombstoned. A turn that reads as high
    is never sent to the summarizer: it stays live, out of the window, and out of search
    (it has no embedding row). The sensitivity check stops once the window is
    full and `min_to_compact` new turns are kept: later turns never reach the
    summarizer. Summaries before the first new turn are not counted toward the window;
    every turn from the first new one on is."""
    candidates = []
    is_summary = []
    kept_chars = 0
    new_turns = 0
    counting = False
    for r in turns:
        window_full = kept_chars > _COMPACT_TRANSCRIPT_CHARS
        if window_full and new_turns >= min_to_compact:
            break
        if r.get("is_saved") or reads_high(str(r["content"])):
            continue
        summary = bool(_compacted_from(r))
        candidates.append(r)
        is_summary.append(summary)
        if not summary:
            new_turns += 1
        counting = counting or not summary
        if counting:
            kept_chars += len(f"{r['role']}: {r['content']}") + 1
    return candidates, is_summary


@staged("compact")
async def handle_conversation_compact(input: ConversationCompactInput) -> ConversationCompactOutput:
    """LLM-summarize the oldest turns before the last `keep_recent` that fit the prompt
    (`_compaction_window`), write the summary as one embedded system message, then
    tombstone the originals and their embeddings.

    The summary row's metadata.compacted_from = [msg_ids] marks it as a summary: a
    later run folds it into its window, and a window of nothing but summaries is
    already compacted. A summary that cannot be embedded compacts nothing
    (`reason="embed-failed"`). A turn that reads as high is left out of the
    window and stays live; a summary that reads as high anyway compacts nothing
    (`reason="summary-high-sensitivity"`).
    """
    import uuid as _uuid

    from server.knowledge.store import conversations as conversation_repo
    from server.llm.client import get_llm_client
    from server.memory.app import extraction_policy
    from server.memory.domain.extraction_policy import with_policy

    rows = await conversation_repo.live_messages(
        input.org, input.namespace, input.agent, input.thread_id
    )
    # A run that failed after tombstoning its turns leaves their embeddings to a retry,
    # whose window no longer holds those turns: the turns this agent's live summaries
    # name that are not live for it get an embeddings pass first. This pass leaves a
    # replaced turn that is still live alone (a run failed at or before the messages
    # mutation; the resent window replaces it), and a row a peer still holds survives.
    live_ids = {str(r["msg_id"]) for r in rows}
    replaced_earlier = sorted({mid for r in rows for mid in _compacted_from(r)} - live_ids)
    if replaced_earlier:
        async with conversation_repo.saved_message_write_lock(
            input.org, input.namespace, input.thread_id
        ):
            await conversation_repo.tombstone_embeddings(
                input.org,
                input.namespace,
                input.agent,
                input.thread_id,
                msg_ids=replaced_earlier,
                when=naive_utc_now(),
            )

    if len(rows) <= input.keep_recent:
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="nothing-to-compact",
        )

    # The sensitivity check reads each candidate turn whole, so it runs off the event loop.
    candidates, is_summary = await asyncio.to_thread(
        _compaction_candidates, rows[: -input.keep_recent], input.min_to_compact
    )
    if not candidates:
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="nothing-to-compact",
        )
    # A re-run: an earlier summary sits where the turns it replaced began, so it is
    # the oldest live row and falls among the candidates. A window of nothing but
    # earlier summaries has no new turn to compact.
    if all(is_summary):
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="already-compacted",
        )
    # An earlier summary in the window is not a new turn: it counts toward neither
    # min_to_compact nor messages_summarized.
    if is_summary.count(False) < input.min_to_compact:
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="below-min-to-compact",
        )

    replaced, transcript = _window_of(candidates, is_summary)
    candidates = [candidates[i] for i in replaced]
    candidate_ids = {str(r["msg_id"]) for r in candidates}

    llm = get_llm_client()
    # The summary replaces the turns it covers in search, so it follows the
    # operator's extraction policy as extraction does.
    system_prompt = with_policy(
        "You are a conversation-history summarizer. Produce a tight "
        "third-person summary of the conversation below, preserving: "
        "named entities, decisions made, open questions, and any "
        "promises or commitments. Drop pleasantries and filler. "
        "Target 200-400 words. No markdown headers. No bullet lists.",
        await extraction_policy.effective(input.org, input.namespace),
    )
    result = await llm.complete(
        system=system_prompt,
        user=f"Conversation excerpt:\n\n{transcript}",
        max_tokens=llm_max_tokens("conversation_compact"),
    )
    summary_text = (result.text or "").strip()
    if not summary_text:
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="summarizer-empty",
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )

    # A summary that reads as high is never embedded, so it is refused before
    # anything is written. The window does not change while its turns stay live, so a
    # retry resends the same transcript and most likely gets the same refusal.
    if reads_high(summary_text):
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="summary-high-sensitivity",
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
    summary_msg_id = f"compact-{_uuid.uuid4().hex[:12]}"
    # The summary is what search finds once the turns it replaces are tombstoned, so
    # it is embedded before the summary or any turn's tombstone is written; without a
    # vector there is no summary and no turn is tombstoned.
    embedded = await _embed_turn(
        summary_text,
        org=input.org,
        namespace=input.namespace,
        thread_id=input.thread_id,
        msg_id=summary_msg_id,
    )
    if embedded.error:
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="embed-failed",
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
    # The summary restates these messages, so it carries their user_id:
    # erasure-by-user tombstones on user_id alone, and a '-' summary would
    # outlive the erased user's messages. A mixed-user window takes the most
    # frequent user; run_id is kept only when the window is one run.
    from collections import Counter

    users = Counter(str(r.get("user_id") or "-") for r in candidates)
    del users["-"]
    summary_user_id = users.most_common(1)[0][0] if users else "-"
    runs = {str(r.get("run_id") or "-") for r in candidates}
    summary_run_id = runs.pop() if len(runs) == 1 else "-"
    now = naive_utc_now()
    # Place the summary where the originals were so reads see it in order.
    earliest_ts = candidates[0]["ts"]

    summary = conversation_repo.MessageInsert(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        thread_id=input.thread_id,
        msg_id=summary_msg_id,
        role="system",
        content=summary_text,
        user_id=summary_user_id,
        run_id=summary_run_id,
        metadata=json.dumps(
            {
                "compacted_from": sorted(candidate_ids),
                "compacted_at": now.isoformat(),
                "llm_provider": llm.provider,
                "llm_model": llm.model,
            }
        ),
        tokens=result.tokens_out,
        ts=earliest_ts,
    )
    await conversation_repo.append(summary)
    await _store_embedding(summary, embedded)
    async with conversation_repo.saved_message_write_lock(
        input.org, input.namespace, input.thread_id
    ):
        await conversation_repo.tombstone_messages(
            input.org,
            input.namespace,
            input.agent,
            input.thread_id,
            sorted(candidate_ids),
            when=now,
        )

    return ConversationCompactOutput(
        compacted=True,
        messages_summarized=sum(not is_summary[i] for i in replaced),
        summary_msg_id=summary_msg_id,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
