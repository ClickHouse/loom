"""Repository for the loom.conversations table.

Conversations are append-only message logs scoped to (org, namespace,
agent, thread_id). One row per message; readers fetch a thread by id
or page through recent threads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict, cast

from pydantic import BaseModel, ConfigDict

from server.core import bm25
from server.core.ch import caller_shaped, own_write_reads, sequential_reads
from server.core.ch_errors import WIDTH_MISMATCH_CODE, error_code
from server.core.ch_params import chunks
from server.core.ch_writes import batched_insert_settings, committed_insert_settings
from server.core.datetimes import naive_utc_now
from server.core.errors import OverloadedError
from server.core.repo_base import _CHClient, _default_client
from server.core.vectors import codes_state
from server.identity.domain.read_scope import ReadFilters, scope_where
from server.knowledge.domain.conversations import (
    EMBEDDED_EXCERPT_CHARS,
    LISTED_MARGIN_CHARS,
    THREAD_EXCERPT_CHARS,
    TITLE_REFUSED,
    ConversationRole,
)
from server.settings import settings


@asynccontextmanager
async def saved_message_write_lock(org: str, namespace: str, thread_id: str) -> AsyncIterator[None]:
    """Serialize saved-state commits and embedding hand-offs for one thread."""
    from server.core.leases import LeaseBusy, LeaseLost, cluster_lease

    identity = json.dumps([org, namespace, thread_id], separators=(",", ":"))
    name = "conversation-saved:" + hashlib.sha256(identity.encode()).hexdigest()
    try:
        async with cluster_lease(name, ttl_s=30, wait_s=30):
            yield
    except (LeaseBusy, LeaseLost) as exc:
        raise OverloadedError("the conversation's saved state is busy; retry") from exc


class MessageRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    org: str
    namespace: str
    agent: str
    thread_id: str
    msg_id: str
    role: ConversationRole
    content: str
    reasoning: str = ""
    user_id: str = "-"
    run_id: str = "-"
    metadata: str = "{}"
    tokens: int = 0
    ts: datetime


class MessageInsert(BaseModel):
    model_config = ConfigDict(frozen=True)

    org: str
    namespace: str
    agent: str
    thread_id: str
    msg_id: str
    role: str
    content: str
    reasoning: str = ""
    user_id: str = "-"
    run_id: str = "-"
    metadata: str = "{}"
    tokens: int = 0
    # Set by conversation.save_message: the user picked this turn for search.
    is_saved: bool = False
    saved_at: datetime | None = None
    ts: datetime | None = None  # CH supplies now() when None


class ThreadRow(TypedDict):
    """A thread's live metadata row as `fetch_thread` reads it."""

    thread_id: str
    title: str
    metadata: str  # JSON-encoded
    run_id: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    agent: str


class ThreadInsertRow(TypedDict):
    """A loom.conversation_threads version row. `tombstoned_at` is sent by the
    writers that replace a row (update, the titler) so the new version stays
    live; conversation.create leaves it to the column default."""

    org: str
    namespace: str
    agent: str
    thread_id: str
    title: str
    metadata: str
    run_id: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    tombstoned_at: NotRequired[datetime | None]


class ConversationEmbeddingRow(TypedDict):
    org: str
    namespace: str
    agent: str
    thread_id: str
    msg_id: str
    role: str
    content_excerpt: str
    embedding_model: str
    embedding_dim: int
    embedding: list[float]
    user_id: str
    run_id: str
    embed_attempts: int
    embed_last_error: str
    embed_failed_at: datetime | None
    is_saved: bool
    saved_at: datetime | None
    ts: datetime


def _hydrate(row: dict[str, Any]) -> MessageRow:
    return MessageRow(
        org=row.get("org", "") or "",
        namespace=row.get("namespace", "") or "",
        agent=row.get("agent", "") or "",
        thread_id=row.get("thread_id", "") or "",
        msg_id=row["msg_id"],
        role=row["role"],
        content=row["content"],
        reasoning=row.get("reasoning", "") or "",
        user_id=row.get("user_id", "-") or "-",
        run_id=row.get("run_id", "-") or "-",
        metadata=row.get("metadata", "{}") or "{}",
        tokens=int(row.get("tokens", 0) or 0),
        ts=row["ts"],
    )


async def append(
    msg: MessageInsert,
    *,
    once_per_identity: bool = False,
    ch: _CHClient | None = None,
) -> None:
    """Append one message. ts defaults to now() at the CH side.

    `once_per_identity` derives the insert's deduplication token from the
    message's identity, so concurrent writers of one (thread_id, msg_id) land
    one row: the table's deduplication window keeps the first block and drops
    the rest. Only within a month's partition, and only while the first block's
    token is still in the window; a block whose rows were since deleted still
    drops a new one, which the caller re-reads to detect."""
    ch = ch or _default_client()
    row: dict[str, Any] = {
        "org": msg.org,
        "namespace": msg.namespace,
        "agent": msg.agent,
        "thread_id": msg.thread_id,
        "msg_id": msg.msg_id,
        "role": msg.role,
        "content": msg.content,
        "reasoning": msg.reasoning,
        "user_id": msg.user_id,
        "run_id": msg.run_id,
        "metadata": msg.metadata,
        "tokens": int(msg.tokens),
        "is_saved": msg.is_saved,
        "saved_at": msg.saved_at,
    }
    if msg.ts is not None:
        row["ts"] = msg.ts
    if once_per_identity:
        # Committed: a replicated table keeps the tokens of committed and of
        # buffered inserts apart, so every writer of an identity sends it alike.
        identity = "\x1f".join((msg.org, msg.namespace, msg.agent, msg.thread_id, msg.msg_id))
        insert_settings: dict[str, Any] = {
            **committed_insert_settings(),
            "insert_deduplication_token": "conversation-turn:"
            + hashlib.sha256(identity.encode()).hexdigest(),
        }
    else:
        insert_settings = batched_insert_settings()
    await ch.insert("loom.conversations", [row], settings=insert_settings)


async def message_exists(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_id: str,
    *,
    live_only: bool = False,
    ch: _CHClient | None = None,
) -> bool:
    """Whether a turn exists, including tombstones unless `live_only` is set.

    Append deduplication includes tombstones so a retry cannot recreate a deleted turn.
    """
    ch = ch or _default_client()
    with own_write_reads():
        row = await ch.query_one(
            """
            SELECT 1 AS ok FROM loom.conversations
            WHERE org = {org:String} AND namespace = {ns:String}
              AND agent = {agent:String} AND thread_id = {tid:String}
              AND msg_id = {mid:String}
              AND (tombstoned_at IS NULL OR NOT {live_only:Bool})
            LIMIT 1
            """,
            params={
                "org": org,
                "ns": namespace,
                "agent": agent,
                "tid": thread_id,
                "mid": msg_id,
                "live_only": live_only,
            },
        )
    return bool(row and row.get("ok"))


class StoredMessage(BaseModel):
    """What conversation.save_message judges a stored (thread_id, msg_id) by,
    over every row it has (a racing append can leave two)."""

    model_config = ConfigDict(frozen=True)

    role: str
    content: str
    run_id: str
    ts: datetime
    saved_at: datetime | None = None
    # Every distinct owner on the rows; '-' is an unowned transcript turn.
    owners: tuple[str, ...]
    tombstoned: bool
    # Rows not yet saved for `user_id` (unflagged, or owned by '-').
    unsaved: int


async def message_state(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_id: str,
    *,
    user_id: str,
    ch: _CHClient | None = None,
) -> StoredMessage | None:
    """The stored rows of (thread_id, msg_id) folded into one verdict, or None
    when no row exists. Matches the rows `message_exists` dedupes on, so
    save_message decides on the same rows an append would skip over."""
    ch = ch or _default_client()
    with own_write_reads():
        row = await ch.query_one(
            """
            SELECT count() AS n,
                   argMax(role, ts) AS role,
                   argMax(content, ts) AS content,
                   argMax(run_id, ts) AS run_id,
                   max(ts) AS last_ts,
                   max(saved_at) AS last_saved_at,
                   groupUniqArray(user_id) AS owners,
                   countIf(tombstoned_at IS NOT NULL) > 0 AS tombstoned,
                   countIf(NOT is_saved OR user_id != {uid:String}) AS unsaved
            FROM loom.conversations
            WHERE org = {org:String} AND namespace = {ns:String}
              AND agent = {agent:String} AND thread_id = {tid:String}
              AND msg_id = {mid:String}
            """,
            params={
                "org": org,
                "ns": namespace,
                "agent": agent,
                "tid": thread_id,
                "mid": msg_id,
                "uid": user_id,
            },
        )
    if not row or not int(row["n"]):
        return None
    return StoredMessage(
        role=str(row["role"]),
        content=str(row["content"]),
        run_id=str(row["run_id"]),
        ts=row["last_ts"],
        saved_at=row.get("last_saved_at"),
        owners=tuple(sorted(str(o) for o in row["owners"])),
        tombstoned=bool(row["tombstoned"]),
        unsaved=int(row["unsaved"]),
    )


class StoredEmbedding(BaseModel):
    """The live embedding rows of one message, as save_message repairs them."""

    model_config = ConfigDict(frozen=True)

    # Rows under the embedder the saved search ranks with that hold a vector; a
    # failed embed's placeholder (embedding_dim 0) is not one.
    current: int
    # Rows of any model not yet saved for `user_id`.
    unsaved: int
    # Rows another user owns, which a save for `user_id` must not take over.
    foreign: int
    # Include retired versions: a repair must replace them even on a slower clock.
    version_ts: datetime | None = None


async def embedding_state(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_id: str,
    *,
    user_id: str,
    embedding_model: str,
    ch: _CHClient | None = None,
) -> StoredEmbedding:
    """Whether the message has an embedding row under `embedding_model`, and how
    many of its live rows still lack the saved flag or the owner."""
    ch = ch or _default_client()
    with own_write_reads():
        row = await ch.query_one(
            """
            WITH agent = {agent:String} AND tombstoned_at IS NULL AS live
            SELECT countIf(live AND embedding_model = {model:String}
                           AND embedding_dim > 0) AS current,
                   countIf(live AND (NOT is_saved OR user_id != {uid:String})) AS unsaved,
                   countIf(live AND user_id NOT IN ('-', '', {uid:String})) AS foreign,
                   maxOrNullIf(ts, embedding_model = {model:String}) AS version_ts
            FROM loom.conversation_embeddings FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND thread_id = {tid:String} AND msg_id = {mid:String}
            """,
            params={
                "org": org,
                "ns": namespace,
                "agent": agent,
                "tid": thread_id,
                "mid": msg_id,
                "uid": user_id,
                "model": embedding_model,
            },
        )
    if not row:
        return StoredEmbedding(current=0, unsaved=0, foreign=0)
    return StoredEmbedding(
        current=int(row["current"]),
        unsaved=int(row["unsaved"]),
        foreign=int(row["foreign"]),
        version_ts=row.get("version_ts"),
    )


async def saved_message_owner(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_id: str,
    *,
    table: str = "loom.conversations",
    ch: _CHClient | None = None,
) -> str | None:
    """The owner of a live, saved row of (thread_id, msg_id), or None. A
    transcript append asks after its embedding row landed: a save that ran
    while the append was embedding marked the rows it could see, not this one."""
    if table not in SAVED_MESSAGE_TABLES:
        raise ValueError(f"not a saved-message table: {table}")
    ch = ch or _default_client()
    final = " FINAL" if table == "loom.conversation_embeddings" else ""
    with own_write_reads():
        row = await ch.query_one(
            f"""
            SELECT user_id FROM {table}{final}
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND agent = {{agent:String}} AND thread_id = {{tid:String}}
              AND msg_id = {{mid:String}} AND is_saved AND tombstoned_at IS NULL
            LIMIT 1
            """,
            params={
                "org": org,
                "ns": namespace,
                "agent": agent,
                "tid": thread_id,
                "mid": msg_id,
            },
        )
    return str(row["user_id"]) if row else None


SAVED_MESSAGE_TABLES = ("loom.conversations", "loom.conversation_embeddings")


async def mark_message_saved(
    table: str,
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_id: str,
    *,
    user_id: str,
    ch: _CHClient | None = None,
) -> None:
    """Flag the live rows of a stored turn in `table` (one of
    SAVED_MESSAGE_TABLES) as saved by `user_id`, leaving every other column —
    run_id included — as written. A synchronous mutation, so the next
    saved-message search sees it; rows already saved for the user are skipped.

    Only rows nobody owns ('-') or `user_id` owns are claimed, in the mutation
    itself: mutations on a table apply in order, so of two saves claiming one
    unowned turn the first takes it and the second matches nothing."""
    if table not in SAVED_MESSAGE_TABLES:
        raise ValueError(f"not a saved-message table: {table}")
    ch = ch or _default_client()
    await ch.execute(
        f"""
        ALTER TABLE {table}
        UPDATE is_saved = true, user_id = {{uid:String}},
               saved_at = coalesce(saved_at, {{at:DateTime64(3, 'UTC')}})
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND agent = {{agent:String}} AND thread_id = {{tid:String}}
          AND msg_id = {{mid:String}} AND tombstoned_at IS NULL
          AND user_id IN ('-', '', {{uid:String}})
          AND (NOT is_saved OR user_id != {{uid:String}})
        SETTINGS mutations_sync = 2
        """,
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "tid": thread_id,
            "mid": msg_id,
            "uid": user_id,
            # bound: a replicated table refuses now64() in a mutation
            "at": naive_utc_now(),
        },
    )


async def unsave_message(
    table: str,
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_id: str,
    *,
    user_id: str,
    remove: bool,
    when: datetime,
    ch: _CHClient | None = None,
) -> None:
    """Withdraw `user_id`'s saved rows of a turn in `table` (one of
    SAVED_MESSAGE_TABLES): clear the flag, or remove an unrecorded transcript
    and retire its vector. Only rows saved by that user change. The synchronous
    mutation makes the next saved-message search omit the turn."""
    if table not in SAVED_MESSAGE_TABLES:
        raise ValueError(f"not a saved-message table: {table}")
    ch = ch or _default_client()
    removing_embedding = remove and table == "loom.conversation_embeddings"
    if removing_embedding:
        # One vector can index another agent's live copy of this turn.
        await tombstone_embeddings(
            org,
            namespace,
            agent,
            thread_id,
            msg_ids=[msg_id],
            when=when,
            preserve_saved=False,
            ch=ch,
        )
    deleting = remove and table == "loom.conversations"
    tombstone = ", tombstoned_at = {when:DateTime}" if removing_embedding else ""
    statement = (
        f"DELETE FROM {table}"
        if deleting
        else f"ALTER TABLE {table} UPDATE is_saved = false, saved_at = NULL{tombstone}"
    )
    sync = "lightweight_deletes_sync" if deleting else "mutations_sync"
    live = "" if removing_embedding else "AND tombstoned_at IS NULL"
    await ch.execute(
        f"""
        {statement}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND agent = {{agent:String}} AND thread_id = {{tid:String}}
          AND msg_id = {{mid:String}} {live}
          AND is_saved AND user_id = {{uid:String}}
        SETTINGS {sync} = 2
        """,
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "tid": thread_id,
            "mid": msg_id,
            "uid": user_id,
            "when": when,
        },
    )


async def list_saved_messages(
    org: str,
    namespace: str,
    agent: str,
    *,
    scope: ReadFilters,
    user_id: str,
    listed: int,
    conversation_id: str | None = None,
    before: tuple[datetime, str, str] | None = None,
    limit: int = 50,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """One page of `user_id`'s live saved messages, newest first, keyed by
    (saved_at, thread_id, msg_id). Grouped per message: a save racing a
    transcript append can leave two rows of one turn, and a page lists it once.
    `before` is the last key of the previous page. `content` is the message's first
    `listed` characters, what the listing checks and cuts."""
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "c.saved_at IS NOT NULL",
        "is_saved",
        "tombstoned_at IS NULL",
        "user_id = {uid:String}",
    ]
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "uid": user_id,
        "lim": int(limit),
        "listed": int(listed),
    }
    if agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if conversation_id:
        where.append("thread_id = {tid:String}")
        params["tid"] = conversation_id
    where.extend(scope_where(scope, params))
    having = ""
    if before is not None:
        having = (
            "HAVING (saved_at, thread_id, msg_id) < "
            "({b_ts:DateTime64(3)}, {b_tid:String}, {b_mid:String})"
        )
        params.update(b_ts=before[0], b_tid=before[1], b_mid=before[2])
    return await ch.query_all(
        f"""
        SELECT thread_id, msg_id, any(role) AS role,
               argMax(substringUTF8(content, 1, {{listed:UInt32}}), ts) AS content,
               max(c.saved_at) AS saved_at
        FROM loom.conversations AS c
        WHERE {" AND ".join(where)}
        GROUP BY thread_id, msg_id
        {having}
        ORDER BY saved_at DESC, thread_id DESC, msg_id DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def saved_message_ids(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_ids: list[str],
    *,
    scope: ReadFilters,
    user_id: str,
    ch: _CHClient | None = None,
) -> set[str]:
    """The ids among `msg_ids` of one thread that `user_id` has live saved rows for."""
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "thread_id = {tid:String}",
        "msg_id IN {mids:Array(String)}",
        "is_saved",
        "tombstoned_at IS NULL",
        "user_id = {uid:String}",
    ]
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "tid": thread_id,
        "mids": list(msg_ids),
        "uid": user_id,
    }
    if agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    where.extend(scope_where(scope, params))
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT msg_id FROM loom.conversations
        WHERE {" AND ".join(where)}
        """,
        params=params,
    )
    return {str(r["msg_id"]) for r in rows}


async def lexical_saved_messages(
    org: str,
    namespace: str,
    agent: str,
    tokens: list[str],
    *,
    scope: ReadFilters,
    user_id: str,
    conversation_id: str | None = None,
    role: str | None = None,
    limit: int = 20,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Okapi BM25 over the whole text of `user_id`'s saved messages, best
    first, rows scoring 0 dropped. The corpus is one user's explicit saves, so
    its statistics (size, average length, per-token document frequency) are
    computed in the same statement rather than kept in a term-frequency view.
    One row per message: a save racing an append can leave two rows of a turn."""
    if not tokens:
        return []
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "is_saved",
        "tombstoned_at IS NULL",
        "user_id = {uid:String}",
    ]
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "uid": user_id,
        "toks": list(tokens),
        "lim": int(limit),
    }
    if agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if conversation_id:
        where.append("thread_id = {tid:String}")
        params["tid"] = conversation_id
    if role:
        where.append("role = {role:String}")
        params["role"] = role
    where.extend(scope_where(scope, params))
    words = f"arrayFilter(t -> length(t) >= {bm25.MIN_TERM_LEN}, splitByNonAlpha(lowerUTF8(body)))"
    score = bm25.bm25_sum_sql(
        "[tf]",
        "[log(1 + (s.n - count() OVER (PARTITION BY word) + 0.5) / "
        "(count() OVER (PARTITION BY word) + 0.5))]",
        dl="dl",
        avgdl="greatest(s.avgdl, 1.0)",
    )
    # Count matching terms as rows; capturing every document's word array in a
    # lambda over query tokens copies that array once per token.
    with caller_shaped():
        return await ch.query_all(
            f"""
            WITH docs AS (
                SELECT thread_id, msg_id, any(role) AS msg_role, argMax(content, ts) AS body,
                       max(ts) AS saved_at, {words} AS words
                FROM loom.conversations
                WHERE {" AND ".join(where)}
                GROUP BY thread_id, msg_id
            ),
            stats AS (
                SELECT count() AS n, avg(length(words)) AS avgdl FROM docs
            ),
            terms AS (
                SELECT thread_id, msg_id, word, count() AS tf,
                       any(dl) AS dl, any(saved_at) AS saved_at
                FROM (
                    SELECT thread_id, msg_id, saved_at, words, length(words) AS dl FROM docs
                )
                ARRAY JOIN words AS word
                WHERE word IN {{toks:Array(String)}}
                GROUP BY thread_id, msg_id, word
            ),
            scored AS (
                SELECT thread_id, msg_id, saved_at, {score} AS term_score
                FROM terms CROSS JOIN stats AS s
            ),
            ranked AS (
                SELECT thread_id, msg_id, max(saved_at) AS saved_at, sum(term_score) AS score
                FROM scored
                GROUP BY thread_id, msg_id
                ORDER BY score DESC, saved_at DESC
                LIMIT {{lim:UInt32}}
            )
            SELECT d.thread_id AS thread_id, d.msg_id AS msg_id, d.msg_role AS role,
                   d.body AS content, d.saved_at AS saved_at, r.score AS score
            FROM ranked AS r INNER JOIN docs AS d USING (thread_id, msg_id)
            ORDER BY score DESC, saved_at DESC
            """,
            params=params,
        )


async def namespace_logging_enabled(
    org: str,
    namespace: str,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Per-namespace conversation-logging switch (the dashboard's 'record my
    conversations' toggle).

    Fails CLOSED: an absent namespace row or a NULL column means "not
    recording". Storing someone's chat transcripts is the kind of thing that
    must be asked for, and the two cases that reach the absent branch — a
    namespace written to before it was ever registered, and a NULL column —
    are both "nobody has expressed a preference": defaulting those to on would
    keep transcripts for a namespace whose owner never answered the question.

    Turn it on per namespace with `conversation.set_logging` or the Record
    conversations switch on the dashboard's Conversations page.
    """
    ch = ch or _default_client()
    r = await ch.query_one(
        """
        SELECT conversation_logging_enabled AS e
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace},
    )
    if r is None or r.get("e") is None:
        return False
    return bool(r["e"])


async def insert_thread(
    row: ThreadInsertRow,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert one loom.conversation_threads row. Used by
    conversation.create — RMT(updated_at) collapses on (org, ns, agent,
    thread_id), so re-create on the same tuple updates title/metadata."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.conversation_threads",
        [cast(dict[str, Any], row)],
        settings=committed_insert_settings(),
    )


async def fetch_thread(
    org: str,
    namespace: str,
    agent: str | None,
    thread_id: str,
    *,
    ch: _CHClient | None = None,
) -> ThreadRow | None:
    """Return the thread-level metadata row, or None if absent. `agent=None`
    reads it whoever owns it (a thread is one row per thread_id).

    `loom.conversation_threads` is ReplacingMergeTree(updated_at) so
    FINAL collapses earlier (title/metadata) versions; we read the
    latest. `metadata` arrives as a JSON-encoded string — callers parse
    if they need structure (the handler does).
    """
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "tid": thread_id}
    agent_clause = ""
    if agent is not None:
        agent_clause = "AND agent = {agent:String}"
        params["agent"] = agent
    row = await ch.query_one(
        f"""
        SELECT thread_id, title, metadata, run_id, user_id, created_at, updated_at, agent
        FROM loom.conversation_threads FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND thread_id = {{tid:String}}
          AND tombstoned_at IS NULL
          {agent_clause}
        LIMIT 1
        """,
        params=params,
    )
    return cast(ThreadRow | None, row)


async def insert_embedding(
    row: ConversationEmbeddingRow,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert one loom.conversation_embeddings row: a turn conversation.append
    embeds, or the summary conversation.compact writes."""
    ch = ch or _default_client()
    insert_settings = embedding_insert_settings(row["embedding"])
    batched = insert_settings.get("async_insert") == 1
    try:
        await ch.insert(
            "loom.conversation_embeddings", [cast(dict[str, Any], row)], settings=insert_settings
        )
    except Exception as e:  # only a shared-flush width error (190) is resent; the rest re-raise
        if not (batched and error_code(e) == WIDTH_MISMATCH_CODE):
            raise
        # Another insert's vector failed the shared flush; this row is sent alone.
        await ch.insert(
            "loom.conversation_embeddings",
            [cast(dict[str, Any], row)],
            settings=committed_insert_settings(),
        )


def embedding_insert_settings(vector: list[float]) -> dict[str, Any]:
    """Batched when the column is plain, or when the vector fits the width the column
    is coded at and the served model writes that width. Committed otherwise (never
    probed, a model change or rebuild pending): a vector of another width fails the
    whole flush it is in (code 190), so it is sent alone."""
    table = "loom.conversation_embeddings"
    if codes_state.is_coded(table, "embedding") is False:
        return batched_insert_settings()
    width = codes_state.stored_width(table, "embedding")
    if width == int(settings.embedding_dim) and len(vector) in (0, width):
        return batched_insert_settings()
    return committed_insert_settings()


async def tombstone_thread(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    *,
    when: datetime,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """Soft-delete one thread: messages + thread row + the embeddings no other agent
    holds (see `tombstone_embeddings`).

    Sets `tombstoned_at = when`; read paths here filter
    `tombstoned_at IS NULL`, so rows vanish from subsequent fetch / list
    / search / count. Returns `{table_name: rows tombstoned}`; for
    loom.conversation_embeddings, turns (one per turn however many embedding models
    hold it).
    """
    ch = ch or _default_client()
    params = {
        "org": org,
        "ns": namespace,
        "agent": agent,
        "tid": thread_id,
        "now": when,
    }
    counts: dict[str, int] = {}

    # 1. Embeddings: every turn the agent logged in the thread, a compacted one too.
    await tombstone_embeddings(org, namespace, agent, thread_id, msg_ids=None, when=when, ch=ch)

    # 2. Messages.
    await ch.execute(
        """
        ALTER TABLE loom.conversations
        UPDATE tombstoned_at = {now:DateTime}
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String}
          AND thread_id = {tid:String} AND tombstoned_at IS NULL
        SETTINGS mutations_sync = 2
        """,
        params=params,
    )
    msg_n = await ch.query_one(
        """
        SELECT count() AS n FROM loom.conversations
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String}
          AND thread_id = {tid:String} AND tombstoned_at IS NOT NULL
        """,
        params=params,
    )
    counts["loom.conversations"] = int(msg_n["n"]) if msg_n else 0

    # 3. Embeddings again, for a turn appended during the delete
    # (`tombstone_embedding_if_message_gone` says why this closes the race).
    await tombstone_embeddings(org, namespace, agent, thread_id, msg_ids=None, when=when, ch=ch)
    # One per turn (FINAL, however many embedding models hold it), earlier calls'
    # tombstones included, as the messages count is. Sequential: the tombstones are
    # inserts, which another replica may not have yet.
    with sequential_reads():
        emb_n = await ch.query_one(
            f"""
            SELECT uniqExact(msg_id) AS n FROM loom.conversation_embeddings FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND thread_id = {{tid:String}} AND tombstoned_at IS NOT NULL
              AND {_THREAD_TURNS}
            """,
            params=params,
        )
    counts["loom.conversation_embeddings"] = int(emb_n["n"]) if emb_n else 0

    # 4. Thread row.
    await ch.execute(
        """
        ALTER TABLE loom.conversation_threads
        UPDATE tombstoned_at = {now:DateTime}
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String}
          AND thread_id = {tid:String} AND tombstoned_at IS NULL
        SETTINGS mutations_sync = 2
        """,
        params=params,
    )
    counts["loom.conversation_threads"] = 1  # at most one thread row per identity tuple

    return counts


# One turn of a thread, however many agents logged it: its id and its content, since a
# msg_id is unique per agent only and a client may number its own turns. The content hash
# also orders two turns that share a millisecond and an id; the page cursor carries only
# (ts, msg_id), so a page boundary that falls between two such turns skips the second.
# The cursor is a wire format the other build of a rollout reads, so carrying the hash
# too is a change under docs/architecture/rolling-upgrades.md, rule 4.
TURN = "msg_id, cityHash64(content)"


async def search_substring(
    org: str,
    namespaces: list[str],
    agent: str,
    query: str,
    *,
    scope: ReadFilters,
    thread_id: str | None = None,
    role: str | None = None,
    run_id: str | None = None,
    user_id: str | None = None,
    saved_only: bool = False,
    limit: int = 50,
    ch: _CHClient | None = None,
) -> list[MessageRow]:
    """Case-insensitive substring search over loom.conversations in
    `namespaces`, in one statement.

    The lexical fallback for conversation.search when the embedder is
    unavailable. Ordering is `ts DESC` (recency-first); cosineDistance
    is not relevant for substring matches.
    """
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace IN {nss:Array(String)}",
        "positionCaseInsensitive(content, {q:String}) > 0",
        "tombstoned_at IS NULL",
    ]
    params: dict[str, Any] = {
        "org": org,
        "nss": list(namespaces),
        "q": query,
        "limit": int(limit),
    }
    # "*"/"-" spans every agent, as the vector search does.
    if agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if thread_id:
        where.append("thread_id = {thread_id:String}")
        params["thread_id"] = thread_id
    if role:
        where.append("role = {role:String}")
        params["role"] = role
    if run_id is not None:
        where.append("run_id = {run_id:String}")
        params["run_id"] = run_id
    if user_id is not None:
        where.append("user_id = {user_id:String}")
        params["user_id"] = user_id
    where.extend(scope_where(scope, params))
    if saved_only:
        where.append("is_saved")

    columns = """org, namespace, agent, thread_id, msg_id, role, content,
               reasoning, user_id, run_id, metadata, tokens, ts"""
    source = f"loom.conversations WHERE {' AND '.join(where)}"
    if agent in ("*", "-"):
        # Across agents a turn two agents logged comes back once, collapsed before the
        # limit so the page holds `limit` turns, on the thread read's key (`TURN`) and as
        # its first copy, the one a thread read shows.
        source = f"""(
            SELECT {columns} FROM {source}
            ORDER BY ts ASC, agent ASC
            LIMIT 1 BY namespace, thread_id, {TURN}
        )"""
    rows = await ch.query_all(
        f"""
        SELECT {columns}
        FROM {source}
        ORDER BY ts DESC
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )
    return [_hydrate(r) for r in rows]


async def list_thread(
    org: str,
    namespace: str,
    agent: str | None,
    thread_id: str,
    *,
    run_id: str | None = None,
    after: tuple[datetime, str] | None = None,
    limit: int = 500,
    order: str = "asc",
    ch: _CHClient | None = None,
) -> list[MessageRow]:
    """Ordered messages within one thread, with optional cursor pagination.

    `after`: `(ts, msg_id)` of the previous page's last row — returns
    only messages strictly after it. `run_id`: restrict to one agent run.
    `agent=None`: every agent's messages in the thread, a turn two agents both
    logged once.
    `order`: `asc` (oldest first, the default) or `desc` (newest first —
    the thread's tail; `after` is an ascending cursor and is not combined
    with it).
    """
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "thread_id = {tid:String}",
        "tombstoned_at IS NULL",
    ]
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "tid": thread_id,
        "limit": int(limit),
    }
    if agent is not None:
        where.append("agent = {agent:String}")
        params["agent"] = agent
    page_where: list[str] = []
    if after is not None:
        ts_after, msg_after = after
        page_where.append(
            "(ts, msg_id) > (toDateTime64({ts:String}, 3, 'UTC'), {mid:String})",
        )
        # A basic-mode `toDateTime64` reads `YYYY-MM-DD hh:mm:ss.fff`; the
        # `+00:00` suffix `isoformat()` puts on an aware datetime is a parse
        # error on the deployed build (26.8.1: code 6), so the cursor is
        # rendered as a naive UTC literal, the way audit.list renders its own.
        if ts_after.tzinfo is not None:
            ts_after = ts_after.astimezone(UTC).replace(tzinfo=None)
        millis = f"{ts_after.microsecond // 1000:03d}"
        params["ts"] = ts_after.strftime("%Y-%m-%d %H:%M:%S.") + millis
        params["mid"] = msg_after
    if run_id is not None:
        where.append("run_id = {run_id:String}")
        params["run_id"] = run_id

    columns = """org, namespace, agent, thread_id, msg_id, role, content,
               reasoning, user_id, run_id, metadata, tokens, ts"""
    if agent is not None:
        # One agent's read pages its own rows as stored, in the one WHERE; its count is
        # the same rows (count_thread_msgs).
        where += page_where
        page_where = []
    source = f"loom.conversations WHERE {' AND '.join(where)}"
    if agent is None:
        # Two agents logging the same turn store it under one content-hashed msg_id. A
        # read across agents keeps each turn's first copy before it pages, so a later
        # copy never slips past the cursor onto the next page. That reads the thread
        # from its start on every page: a thread is one key prefix, and the copy before
        # the cursor is what says a later one is not a new turn.
        source = f"""(
            SELECT {columns} FROM {source}
            ORDER BY ts ASC, msg_id ASC, cityHash64(content) ASC, agent ASC
            LIMIT 1 BY {TURN}
        )"""
    rows = await ch.query_all(
        f"""
        SELECT {columns}
        FROM {source}
        {"WHERE " + " AND ".join(page_where) if page_where else ""}
        ORDER BY ts {direction}, msg_id {direction}, cityHash64(content) {direction}
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )
    return [_hydrate(r) for r in rows]


async def count_thread_msgs(
    org: str,
    namespace: str,
    agent: str | None,
    thread_id: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Turns in one (org, ns, agent, thread_id): the rows its list pages
    through. `agent=None` counts every agent's distinct turns (`TURN`): two
    agents logging the same turn store it under one content-hashed id, which a
    read across agents shows once.

    Used by `conversation.get` to populate `msg_count` whether or not
    the caller asked for the messages payload.
    """
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "tid": thread_id}
    agent_clause = ""
    if agent is not None:
        agent_clause = "AND agent = {agent:String}"
        params["agent"] = agent
    turns = "count()" if agent is not None else f"uniqExact({TURN})"
    row = await ch.query_one(
        f"""
        SELECT {turns} AS n
        FROM loom.conversations
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND thread_id = {{tid:String}}
          AND tombstoned_at IS NULL
          {agent_clause}
        """,
        params=params,
    )
    return int(row["n"]) if row else 0


async def recent_threads(
    org: str,
    namespace: str,
    *,
    window_days: int,
    limit: int,
    offset: int = 0,
    agent: str | None = None,
    count_turns: bool = True,
    last_turn: bool = True,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The namespace's threads with a live message in the window. Most recent first, ties
    by thread_id; `offset` skips that many. Each carries its message count and first/last
    seen. `count_turns`: the message count is the thread's distinct turns, as an opened
    thread shows; off, the window's raw row count (for a caller that does not read the
    count). `last_turn`: the latest message's agent (`last_agent`), role and first
    `THREAD_EXCERPT_CHARS + LISTED_MARGIN_CHARS` characters (`last_content`, so the listing
    can check them for a high shape before it cuts the excerpt); off, they are empty. With
    both off the page is one statement."""
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "wd": window_days,
        "lim": limit,
        "off": offset,
    }
    agent_clause = ""
    if agent:
        agent_clause = " AND agent = {agent:String}"
        params["agent"] = agent
    rows = await ch.query_all(
        f"""
        SELECT
            thread_id,
            count()                                          AS msg_count,
            min(ts)                                          AS first_seen,
            max(ts)                                          AS last_seen
        FROM loom.conversations
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND ts > now() - toIntervalDay({{wd:UInt32}})
          AND tombstoned_at IS NULL
          {agent_clause}
        GROUP BY thread_id
        ORDER BY last_seen DESC, thread_id ASC
        LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}
        """,
        params=params,
    )
    blank = {"last_agent": "", "last_role": "", "last_content": ""}
    if not rows or not (count_turns or last_turn):
        return [{**r, **blank} for r in rows]
    # The count an opened thread shows (each thread whole, one per turn however many agents
    # logged it) and the last turn, for the page's threads only: an exact distinct count,
    # or every thread's last turn, over the whole window would hold every turn's key or
    # text in memory to rank threads it then drops. The last turn's agent, role and text
    # come from one argMax, `msg_id` breaking a `ts` tie, so they name the same turn.
    selected = ["thread_id"]
    if count_turns:
        count = "count()" if agent else f"uniqExact({TURN})"
        selected.append(f"{count} AS n")
    if last_turn:
        selected.append(
            "argMax((agent, toString(role), substringUTF8(content, 1, {listed:UInt32})),"
            " (ts, msg_id)) AS last_turn"
        )
    counted = await ch.query_all(
        f"""
        SELECT {", ".join(selected)}
        FROM loom.conversations
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND thread_id IN {{tids:Array(String)}}
          AND tombstoned_at IS NULL
          {agent_clause}
        GROUP BY thread_id
        """,
        params={
            **params,
            "tids": [r["thread_id"] for r in rows],
            "listed": THREAD_EXCERPT_CHARS + LISTED_MARGIN_CHARS,
        },
    )
    by_thread = {r["thread_id"]: r for r in counted}
    out = []
    for r in rows:
        keyed = by_thread.get(r["thread_id"])
        if keyed is None:
            continue  # its last live turn was deleted between the two statements
        last_agent, last_role, last_content = keyed["last_turn"] if last_turn else ("", "", "")
        out.append(
            {
                **r,
                "msg_count": keyed["n"] if count_turns else r["msg_count"],
                "last_agent": last_agent,
                "last_role": last_role,
                "last_content": last_content,
            }
        )
    return out


async def thread_titles(
    org: str,
    namespace: str,
    thread_ids: list[str],
    *,
    ch: _CHClient | None = None,
) -> dict[str, str]:
    """thread_id → title of the live metadata rows among `thread_ids`. A point
    lookup, not a JOIN: FINAL on a JOIN side trips the new analyzer."""
    if not thread_ids:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT thread_id, title FROM loom.conversation_threads FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND thread_id IN {ids:Array(String)} AND tombstoned_at IS NULL
        """,
        params={"org": org, "ns": namespace, "ids": thread_ids},
    )
    return {str(t["thread_id"]): str(t.get("title") or "") for t in rows}


async def live_messages(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Every live message of one thread, oldest first, a compaction summary ahead of
    the turns at its `ts` (it sits at its first source's `ts`, so it leads the window it
    was written over). loom.conversations is a plain MergeTree: FINAL is illegal there
    (code 181), and conversation.append's existence check keeps msg_ids unique."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT msg_id, role, content, ts, metadata, user_id, run_id, is_saved
        FROM loom.conversations
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND thread_id = {tid:String}
          AND tombstoned_at IS NULL
        ORDER BY ts ASC,
          NOT (role = 'system' AND notEmpty(JSONExtractArrayRaw(metadata, 'compacted_from'))),
          msg_id ASC
        """,
        params={"org": org, "ns": namespace, "agent": agent, "tid": thread_id},
    )


async def tombstone_messages(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_ids: list[str],
    *,
    when: datetime,
    ch: _CHClient | None = None,
) -> None:
    """Tombstone the named messages of one thread and their embeddings, so
    conversation.search stops returning them too. The messages mutation is
    synchronous, so the next read sees a coherent thread. Saved rows are checked
    by the mutation itself, including saves made while the summary was generated.
    Embeddings are tombstoned before the messages, so a retry repeats both; a
    second pass closes the race `tombstone_embedding_if_message_gone` describes."""
    ch = ch or _default_client()
    await tombstone_embeddings(org, namespace, agent, thread_id, msg_ids=msg_ids, when=when, ch=ch)
    for part in chunks(msg_ids):
        await ch.execute(
            """
            ALTER TABLE loom.conversations
            UPDATE tombstoned_at = {now:DateTime}
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND agent = {agent:String}
              AND thread_id = {tid:String}
              AND msg_id IN {ids:Array(String)}
              AND NOT is_saved
            SETTINGS mutations_sync = 2
            """,
            params={
                "now": when,
                "org": org,
                "ns": namespace,
                "agent": agent,
                "tid": thread_id,
                "ids": part,
            },
        )
    await tombstone_embeddings(org, namespace, agent, thread_id, msg_ids=msg_ids, when=when, ch=ch)


async def tombstone_embedding_if_message_gone(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_id: str,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Retire a late embedding if its source was removed while embedding ran.

    Unsave deletes unrecorded transcripts; delete and compaction leave tombstones.
    Reconciliation keeps a live peer's vector and identity in either case.
    """
    ch = ch or _default_client()
    with sequential_reads():
        row = await ch.query_one(
            """
            SELECT count() AS n, max(tombstoned_at) AS gone FROM loom.conversations
            WHERE org = {org:String} AND namespace = {ns:String} AND agent = {agent:String}
              AND thread_id = {tid:String} AND msg_id = {mid:String}
            """,
            params={"org": org, "ns": namespace, "agent": agent, "tid": thread_id, "mid": msg_id},
        )
    if row and (not row["n"] or row["gone"] is not None):
        await tombstone_embeddings(
            org,
            namespace,
            agent,
            thread_id,
            msg_ids=[msg_id],
            when=row["gone"] or datetime.now(UTC),
            ch=ch,
        )


# Every embedding row of an agent's turns in a thread: rows it wrote (one whose message
# row is gone included: the conversation retention TTL expires message rows only), and
# rows of a turn it logged that another agent wrote last.
_THREAD_TURNS = """(agent = {agent:String} OR msg_id IN (
            SELECT msg_id FROM loom.conversations
            WHERE org = {org:String} AND namespace = {ns:String}
              AND agent = {agent:String} AND thread_id = {tid:String}
          ))"""


# Rows written by build N-1 and by earlier builds hold the first 240 characters of the
# turn, and a re-embed keeps that excerpt. A row's excerpt is compared whole against a
# peer's content cut at this length and at EMBEDDED_EXCERPT_CHARS. It is not
# conversation_search_excerpt_chars, the cut a search hit shows.
_SHORT_EXCERPT_CHARS = 240  # not a setting: stored rows are matched at this length


# Live copies of the thread's turns that search indexes (user and
# assistant turns, and compaction summaries); `full` is the text append embeds.
_PEER_TURNS = """
    SELECT msg_id, agent, role, run_id, user_id, is_saved, saved_at, ts,
      substringUTF8(content, 1, {embedded:UInt32}) AS full
    FROM loom.conversations
    WHERE org = {org:String} AND namespace = {ns:String}
      AND thread_id = {tid:String}
      AND (agent != {agent:String} OR ({preserve_saved:Bool} AND is_saved))
      AND tombstoned_at IS NULL
      AND (role IN ('user', 'assistant')
           OR (role = 'system' AND notEmpty(JSONExtractArrayRaw(metadata, 'compacted_from'))))
"""


# Each peer copy with the text a row would hold for it at either excerpt length.
_PEER_COPIES = (
    "SELECT msg_id, agent, arrayJoin([full, substringUTF8(full, 1, {short:UInt32})]) AS excerpt"
    " FROM (" + _PEER_TURNS + ")"
)


_RECONCILE_ROUNDS = 8


def _unheld_embeddings(turns: str) -> str:
    """The thread's live embedding rows of the turns `turns` selects whose own agent
    holds no peer copy of them: `agent`'s rows, and rows handed to a peer that has
    since removed its copy."""
    return (
        "SELECT * FROM loom.conversation_embeddings FINAL"
        " WHERE org = {org:String} AND namespace = {ns:String}"
        " AND thread_id = {tid:String} AND tombstoned_at IS NULL AND "
        + turns
        + " AND (msg_id, content_excerpt, agent) NOT IN"
        " (SELECT msg_id, excerpt, agent FROM (" + _PEER_COPIES + "))"
    )


def _reconcile_sql(turns: str) -> str:
    """A version row for each unheld row: passed to the latest peer copy that holds
    it (its agent, role, run and user, so it answers that copy's searches and
    erasure), or tombstoned when none does. `matched` marks a join match: an unmatched
    row reads its default, 0 (any field of `holder`, the agent included, can be
    empty).

    The copy must hold the text the row's vector was built from (`embedded`). A row
    holding the 240-character cut of its own agent's message (a tombstoned one keeps
    its content) is taken as built from that message's longer cut, as append built it;
    a re-embed built it from the 240 characters, which the row cannot show, so a peer
    sharing only those does not take it. One holding exactly 240 characters with no
    such message to check could have been built from more, so it passes to nobody
    (`known = 0`)."""
    keep = "p.matched = 1 AND e.known = 1"
    own = "s.found = 1 AND u.content_excerpt = substringUTF8(s.src, 1, {short:UInt32})"
    return (
        "INSERT INTO loom.conversation_embeddings"
        " SELECT e.* EXCEPT (embedded, known) REPLACE ("
        f"if({keep}, p.holder.1, e.agent) AS agent, if({keep}, p.holder.2, e.role) AS role,"
        f" if({keep}, p.holder.3, e.run_id) AS run_id,"
        f" if({keep}, p.holder.4, e.user_id) AS user_id,"
        f" if({keep}, p.holder.5, e.is_saved) AS is_saved,"
        f" if({keep}, p.holder.6, e.saved_at) AS saved_at,"
        f" if({keep}, NULL, {{now:DateTime}}) AS tombstoned_at)"
        f" FROM (SELECT u.*, if({own}, s.src, u.content_excerpt) AS embedded,"
        f" ({own}) OR lengthUTF8(u.content_excerpt) != {{short:UInt32}} AS known"
        " FROM (" + _unheld_embeddings(turns) + ") AS u"
        " LEFT JOIN (SELECT agent, msg_id, 1 AS found,"
        " any(substringUTF8(content, 1, {embedded:UInt32})) AS src FROM loom.conversations"
        " WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String}"
        " GROUP BY agent, msg_id) AS s ON u.agent = s.agent AND u.msg_id = s.msg_id) AS e"
        " LEFT JOIN (SELECT msg_id, full, 1 AS matched,"
        " argMax((agent, role, run_id, user_id, is_saved, saved_at), ts) AS holder"
        " FROM (" + _PEER_TURNS + ") GROUP BY msg_id, full) AS p"
        " ON e.msg_id = p.msg_id AND e.embedded = p.full"
    )


async def tombstone_embeddings(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    *,
    msg_ids: list[str] | None,
    when: datetime,
    preserve_saved: bool = True,
    ch: _CHClient | None = None,
) -> None:
    """Tombstone the embeddings of `agent`'s turns in one thread: `msg_ids`, or every
    turn it logged there (`_THREAD_TURNS`) plus the rows whose own agent holds no live
    message for the turn (a late hand-off, a compaction by an earlier build, or a
    message the retention TTL expired).

    The embeddings key has no agent, so a turn several agents logged is one row. It
    stays while another agent holds a live copy with the row's text, and passes to that
    copy when its own agent holds none. The reconcile copies the row with its `ts`, so
    the copy replaces it in its partition (the later insert wins a `ts` tie)."""
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "now": when,
        "org": org,
        "ns": namespace,
        "agent": agent,
        "tid": thread_id,
        "embedded": EMBEDDED_EXCERPT_CHARS,
        "short": _SHORT_EXCERPT_CHARS,
        "preserve_saved": preserve_saved,
    }
    # Sequential reads: on a multi-replica service a lagging replica could miss a turn
    # appended just before, leaving its embedding live once its message is tombstoned.
    with sequential_reads():
        if msg_ids is None:
            # The rows `_THREAD_TURNS` selects, by key, read once: a row selected only
            # through `agent` (its message row gone) stops matching once handed to a
            # peer, and the re-read must still see it if that hand-off lands late. Rows
            # whose own agent holds no live message for the turn (deleted, compacted or
            # expired) are added: a late hand-off can land one there after this call's
            # snapshot, and the resend reaches it only this way, even once retention has
            # removed that agent's message row. A writer stores the message before its
            # embedding, so no row being written matches.
            rows = await ch.query_all(
                "SELECT DISTINCT embedding_model, msg_id FROM loom.conversation_embeddings FINAL"
                " WHERE org = {org:String} AND namespace = {ns:String}"
                " AND thread_id = {tid:String} AND tombstoned_at IS NULL AND ("
                + _THREAD_TURNS
                + " OR (agent, msg_id) NOT IN (SELECT agent, msg_id FROM loom.conversations"
                " WHERE org = {org:String} AND namespace = {ns:String}"
                " AND thread_id = {tid:String} AND tombstoned_at IS NULL))",
                params=params,
            )
            turns = "(embedding_model, msg_id) IN {keys:Array(Tuple(String, String))}"
            keys = [(str(r["embedding_model"]), str(r["msg_id"])) for r in rows]
            parts = [{**params, "keys": p} for p in chunks(keys)]
        else:
            turns = "msg_id IN {ids:Array(String)}"
            parts = [{**params, "ids": p} for p in chunks(msg_ids)]
        for bound in parts:
            # A reconcile can read a peer's copy as live and land after that peer's whole
            # delete. So it repeats until a read taken after its last write finds no
            # unheld row; a round goes again only when a write landed on these turns
            # between its reconcile and that read.
            for _ in range(_RECONCILE_ROUNDS):
                await ch.execute(_reconcile_sql(turns), bound)
                if not await ch.query_all(
                    "SELECT 1 FROM (" + _unheld_embeddings(turns) + ") LIMIT 1", params=bound
                ):
                    break
            else:
                raise OverloadedError(
                    f"embeddings of thread {thread_id} kept changing; retry", retry_after_s=1
                )


async def titler_candidates(
    *,
    window_hours: int,
    min_turns: int,
    limit: int,
    exclude_orgs: frozenset[str] = frozenset(),
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Recent threads with at least `min_turns` live messages in namespaces that record
    conversations, untitled, not deleted and not marked `title_refused`, most recent first;
    none of `exclude_orgs` (orgs whose model calls are switched off)."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT c.org AS org, c.namespace AS namespace, c.thread_id AS thread_id,
               argMax(c.agent, c.ts) AS agent, count() AS n, min(c.ts) AS created_at
        FROM loom.conversations AS c
        INNER JOIN loom.namespaces AS n FINAL
                ON c.org = n.org AND c.namespace = n.namespace
        WHERE n.active = true AND n.conversation_logging_enabled = true
          AND c.tombstoned_at IS NULL
          AND c.ts > now() - toIntervalHour({wh:UInt32})
          AND NOT has({off:Array(String)}, c.org)
          -- Titled and deleted threads are not candidates: skipped after
          -- the LIMIT, they filled it every pass and older untitled
          -- threads were never reached. Nor is a thread marked
          -- title_refused: its title, or every opening turn, read as high.
          AND (c.org, c.namespace, c.thread_id) NOT IN (
              SELECT org, namespace, thread_id FROM loom.conversation_threads FINAL
              WHERE tombstoned_at IS NOT NULL OR title != ''
                 OR JSONExtractBool(metadata, {refused:String})
          )
        GROUP BY c.org, c.namespace, c.thread_id
        HAVING n >= {mt:UInt32}
        ORDER BY max(c.ts) DESC
        LIMIT {lim:UInt32}
        """,
        params={
            "wh": window_hours,
            "mt": min_turns,
            "lim": limit,
            "off": sorted(exclude_orgs),
            "refused": TITLE_REFUSED,
        },
    )


_THREAD_COLUMNS = (
    "org, namespace, agent, thread_id, title, metadata, run_id, user_id, "
    "created_at, updated_at, tombstoned_at"
)


async def thread_rows(
    keys: list[tuple[str, str, str]], *, ch: _CHClient | None = None
) -> dict[tuple[str, str, str], ThreadInsertRow]:
    """The live version of each thread's metadata row, tombstoned or not, in
    one statement, keyed by (org, namespace, thread_id).

    No `tombstoned_at` filter: a deleted thread must read as deleted, not as
    untitled — otherwise a title write (tombstoned_at unset, newer
    updated_at) resurrects it under FINAL. Every column is read because a
    title write is a new ReplacingMergeTree(updated_at) version and must carry
    all of them forward.
    """
    if not keys:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT {_THREAD_COLUMNS} FROM loom.conversation_threads FINAL
        WHERE (org, namespace, thread_id) IN {{keys:Array(Tuple(String, String, String))}}
        """,
        params={"keys": keys},
    )
    return {
        (str(r["org"]), str(r["namespace"]), str(r["thread_id"])): cast("ThreadInsertRow", r)
        for r in rows
    }


async def opening_turns(
    org: str, namespace: str, thread_id: str, *, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The first live user/assistant turns of a thread (any agent), oldest first."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT role, content FROM loom.conversations
        WHERE org = {o:String} AND namespace = {ns:String} AND thread_id = {tid:String}
          AND tombstoned_at IS NULL AND role IN ('user', 'assistant')
        ORDER BY ts ASC, msg_id ASC
        LIMIT {lim:UInt32}
        """,
        params={"o": org, "ns": namespace, "tid": thread_id, "lim": limit},
    )
