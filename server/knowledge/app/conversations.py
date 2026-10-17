"""conversation.{create,append,update,delete,get,list,search,...} handlers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.auth.fanout import namespaces_to_search
from server.core.cursors import decode_cursor as _decode_cursor
from server.core.cursors import encode_cursor as _encode_cursor
from server.core.datetimes import naive_utc_now
from server.embedding import get_embedder
from server.identity.app.conversation_logging import set_conversation_logging
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
    ConversationSearchInput,
    ConversationSearchOutput,
    ConversationSetLoggingInput,
    ConversationSetLoggingOutput,
    ConversationThreadsInput,
    ConversationThreadsOutput,
    ConversationThreadSummary,
    ConversationUpdateInput,
    ConversationUpdateOutput,
    _ConversationEntry,
    _ConversationHit,
)
from server.observability.genai import staged
from server.observability.otel import traced
from server.operations.read_projection import project_rows

if TYPE_CHECKING:
    from server.knowledge.app.conversation_search import ConversationHit
    from server.knowledge.store.conversations import ConversationEmbeddingRow

log = logging.getLogger(__name__)

_EXCERPT_LIMIT = 240
_EMBED_EXCERPT_LIMIT = 2048


async def namespace_recording_enabled(org: str, namespace: str) -> bool:
    """The namespace owner's "record my conversations" switch. Fails closed: a
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
    """Insert one message row + one embedding row.

    Embedding failure does not block the append — the message still lands
    with `embed_attempts=0` and the dead-letter columns populated, so a
    follow-up worker can retry without losing data.
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

    # Per-namespace logging switch (the dashboard toggle). When off, accept the call
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
    # conversation.search ranking and wastes embed spend.
    if input.role not in ("user", "assistant"):
        with traced("ch.insert.conversation", role=input.role):
            await conversation_repo.append(msg_row)
        return ConversationAppendOutput(msg_id=msg_id, ts=now)

    embedder = get_embedder()
    excerpt = input.content[:_EMBED_EXCERPT_LIMIT]

    async def _insert_message() -> None:
        with traced("ch.insert.conversation", role=input.role):
            await conversation_repo.append(msg_row)

    async def _compute_embedding() -> tuple[list[float], int, str]:
        """Fail-open: a failed embed still stores the turn, with the dead-letter
        columns set so the embedding worker can retry it."""
        try:
            with traced(
                "embed.compute.conversation",
                model=embedder.model_name,
                excerpt_len=len(excerpt),
            ):
                vec = await embedder.embed(excerpt)
            return vec, embedder.dim, ""
        except Exception as e:  # noqa: BLE001 — embed failure still stores the turn for retry
            log.warning(
                "conversation embed failed (org=%s ns=%s thread=%s msg=%s): %s",
                input.org,
                input.namespace,
                input.thread_id,
                msg_id,
                e,
            )
            return [], 0, f"{type(e).__name__}: {e}"

    # Concurrent because they are independent: neither the transcript row nor the vector
    # depends on the other, and run sequentially the ~66 ms insert would sit in front of a
    # ~210 ms embed. Both still complete before the response, so conversation.search keeps
    # its read-your-write guarantee.
    _, (embedding, embedding_dim, embed_err) = await asyncio.gather(
        _insert_message(), _compute_embedding()
    )

    embedding_row: ConversationEmbeddingRow = {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "thread_id": input.thread_id,
        "msg_id": msg_id,
        "role": input.role,
        "content_excerpt": input.content[:_EXCERPT_LIMIT],
        "embedding_model": embedder.model_name,
        "embedding_dim": embedding_dim,
        "embedding": embedding,
        "user_id": input.user_id,
        "run_id": input.run_id,
        "embed_attempts": 1 if embedding_dim > 0 else 0,
        "embed_last_error": embed_err,
        "embed_failed_at": now if embed_err else None,
        "ts": now,
    }
    with traced("ch.insert.conversation_embedding", embedded=embedding_dim > 0):
        await conversation_repo.insert_embedding(embedding_row)
    return ConversationAppendOutput(msg_id=msg_id, ts=now)


async def handle_conversation_set_logging(
    input: ConversationSetLoggingInput,
) -> ConversationSetLoggingOutput:
    """Toggle the per-namespace conversation-logging switch (the dashboard's 'record
    my conversations' control); conversation.append enforces it (no-op when off).
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

    if input.title is None and input.metadata is None and input.run_id is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_FIELDS_PROVIDED",
                "message": (
                    "conversation.update requires at least one of `title`, `metadata`, or `run_id`."
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
    merged_run_id = input.run_id if input.run_id is not None else existing.get("run_id", "-") or "-"

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
    """Soft-delete one thread (messages + thread row + embeddings).

    Tombstone, not hard-delete: rows disappear from reader queries
    immediately because the read paths filter `tombstoned_at IS NULL`;
    the retention TTL hard-deletes them later. For user/namespace-wide
    erasure use `erasure.request_user_id` / `erasure.request_namespace`.
    """
    from datetime import UTC

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

    now = datetime.now(UTC).replace(tzinfo=None)
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
    """The agent filter for one thread's reads: "*" reads every agent's turns
    (a caller that has only the thread id: a search hit, a memory's source)."""
    return None if agent == "*" else agent


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
                metadata=_safe_load_json(r.metadata),
                tokens=r.tokens,
                ts=r.ts,
            )
            for r in page
        ]

    return ConversationGetOutput(
        found=True,
        thread_id=input.thread_id,
        title=thread_row.get("title", "") or "",
        metadata=_safe_load_json(thread_row.get("metadata", "") or ""),
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
        run_id=input.run_id,
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
            metadata=_safe_load_json(r.metadata),
            tokens=r.tokens,
            ts=r.ts,
        )
        for r in page
    ]
    next_cursor = _encode_cursor(page[-1].ts, page[-1].msg_id) if page and truncated else None
    return ConversationListOutput(rows=entries, next_cursor=next_cursor)


async def handle_conversation_search(
    input: ConversationSearchInput,
) -> ConversationSearchOutput:
    """Hybrid semantic + filter search.

    Embed the query, rank `loom.conversation_embeddings` by cosine
    distance with recency tiebreak. Falls back to substring search when
    the embedder is unavailable so the endpoint never 5xx's.
    """
    import asyncio

    from server.knowledge.app.conversation_search import (
        ConversationVectorQuery,
        vector_search,
    )
    from server.settings import settings

    embedder = get_embedder()
    max_distance = 1.0 - settings.conversation_search_min_similarity
    query_vec: list[float] | None
    try:
        query_vec = await embedder.embed(input.query[:_EMBED_EXCERPT_LIMIT])
    except Exception as e:  # noqa: BLE001 — embed failure falls back to substring search
        log.warning("conversation embed query failed; falling back to substring: %s", e)
        query_vec = None

    namespaces, skipped = await namespaces_to_search(
        input.org, input.namespace, input.namespace_scope
    )
    if query_vec is None:
        return await _search_substring_fallback(input, namespaces, skipped)

    async def _search_one(ns: str) -> tuple[str, list[ConversationHit]]:
        return ns, await vector_search(
            ConversationVectorQuery(
                org=input.org,
                namespace=ns,
                agent=input.agent,
                embedding_model=embedder.model_name,
                query_vec=query_vec,
                top_k=input.top_k,
                thread_id=input.thread_id or None,
                role=input.role or None,
                run_id=input.run_id,
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
    input: ConversationSearchInput, namespaces: list[str], skipped: list[str]
) -> ConversationSearchOutput:
    """Lexical fallback used when the embedder is unavailable: the same
    namespaces, newest match first."""
    from server.knowledge.store import conversations as conversation_repo

    rows = await conversation_repo.search_substring(
        input.org,
        namespaces,
        input.agent,
        input.query,
        thread_id=input.thread_id or None,
        role=input.role or None,
        run_id=input.run_id,
        limit=input.top_k,
    )
    hits = [
        _ConversationHit(
            thread_id=r.thread_id,
            msg_id=r.msg_id,
            role=r.role,
            content_excerpt=(
                r.content[:_EXCERPT_LIMIT] + "…" if len(r.content) > _EXCERPT_LIMIT else r.content
            ),
            ts=r.ts,
            namespace=r.namespace,
        )
        for r in rows
    ]
    return ConversationSearchOutput(hits=hits, namespaces_skipped=skipped)


def _safe_load_json(s: Any) -> dict[str, Any]:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


async def handle_conversation_threads(input: ConversationThreadsInput) -> ConversationThreadsOutput:
    """Recent thread list grouped over loom.conversations.

    Group by thread_id, aggregate message count + first/last seen,
    project the most recent message's role + content excerpt so the
    Sessions view has something to click without typing a query.
    """
    from server.knowledge.store import conversations as conversation_repo

    rows = await conversation_repo.recent_threads(
        input.org,
        input.namespace,
        window_days=int(input.window_days),
        limit=int(input.limit),
        agent=input.agent if input.agent and input.agent != "*" else None,
    )
    # Titles live on the conversation_threads metadata table (set by
    # conversation.create).
    titles = await conversation_repo.thread_titles(
        input.org, input.namespace, [str(r["thread_id"]) for r in rows]
    )
    threads = [
        ConversationThreadSummary(
            thread_id=str(r["thread_id"]),
            agent=str(r["last_agent"]),
            msg_count=int(r["msg_count"]),
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
            last_role=str(r["last_role"]),
            last_content_excerpt=str(r["last_content_excerpt"]),
            title=titles.get(str(r["thread_id"]), ""),
        )
        for r in rows
    ]
    project_rows(threads, ConversationThreadSummary, input.fields, always=frozenset({"thread_id"}))
    return ConversationThreadsOutput(threads=threads)


# The summarizer's prompt budget, in characters of `role: content` lines.
_COMPACT_TRANSCRIPT_CHARS = 16000


def _fitting_prefix(lines: list[str], cap: int) -> int:
    """How many leading lines fit in `cap` characters joined by newlines;
    at least one, so a single oversized message is still compacted (clipped)."""
    used = 0
    for i, line in enumerate(lines):
        used += len(line) + (1 if i else 0)
        if used > cap:
            return max(1, i)
    return len(lines)


@staged("compact")
async def handle_conversation_compact(input: ConversationCompactInput) -> ConversationCompactOutput:
    """LLM-summarize messages older than `keep_recent`, tombstone the
    originals, write the summary as one synthetic system message.

    The new summary row carries metadata.compacted_from = [msg_ids]
    so reruns can detect the candidate window is already covered.
    """
    import json as _json
    import uuid as _uuid
    from datetime import UTC, datetime

    from server.knowledge.store import conversations as conversation_repo
    from server.llm.client import get_llm_client

    rows = await conversation_repo.live_messages(
        input.org, input.namespace, input.agent, input.thread_id
    )

    if len(rows) <= input.keep_recent:
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="nothing-to-compact",
        )

    candidates = rows[: -input.keep_recent]
    if len(candidates) < input.min_to_compact:
        return ConversationCompactOutput(
            compacted=False,
            messages_summarized=0,
            reason="below-min-to-compact",
        )

    # Idempotency guard: bail if a kept system message already covers
    # every candidate msg_id via compacted_from (this is a re-run).
    kept = rows[-input.keep_recent :]
    candidate_ids = {str(r["msg_id"]) for r in candidates}
    for k in kept:
        if k.get("role") != "system":
            continue
        try:
            meta = _json.loads(k.get("metadata") or "{}")
        except (ValueError, TypeError):
            meta = {}
        prev = set(meta.get("compacted_from") or [])
        if prev and candidate_ids.issubset(prev):
            return ConversationCompactOutput(
                compacted=False,
                messages_summarized=0,
                reason="already-compacted",
            )

    # The summary replaces exactly the messages the summarizer read: the window
    # is cut to the oldest messages whose lines fit the prompt, and the rest stay
    # verbatim for the next run (a message past the cap was never summarized, so
    # tombstoning it would drop it).
    lines = [f"{r['role']}: {r['content']}" for r in candidates]
    fit = _fitting_prefix(lines, _COMPACT_TRANSCRIPT_CHARS)
    candidates = candidates[:fit]
    candidate_ids = {str(r["msg_id"]) for r in candidates}
    transcript = "\n".join(lines[:fit])[:_COMPACT_TRANSCRIPT_CHARS]

    llm = get_llm_client()
    system_prompt = (
        "You are a conversation-history summarizer. Produce a tight "
        "third-person summary of the conversation below, preserving: "
        "named entities, decisions made, open questions, and any "
        "promises or commitments. Drop pleasantries and filler. "
        "Target 200-400 words. No markdown headers. No bullet lists."
    )
    result = await llm.complete(
        system=system_prompt,
        user=f"Conversation excerpt:\n\n{transcript}",
        max_tokens=600,
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

    summary_msg_id = f"compact-{_uuid.uuid4().hex[:12]}"
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
    now = datetime.now(UTC).replace(tzinfo=None)
    # Place the summary where the originals were so reads see it in order.
    earliest_ts = candidates[0]["ts"]

    await conversation_repo.append(
        conversation_repo.MessageInsert(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            thread_id=input.thread_id,
            msg_id=summary_msg_id,
            role="system",
            content=summary_text,
            user_id=summary_user_id,
            run_id=summary_run_id,
            metadata=_json.dumps(
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
    )
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
        messages_summarized=len(candidates),
        summary_msg_id=summary_msg_id,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
