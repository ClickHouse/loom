"""Repository for the loom.conversations table.

Conversations are append-only message logs scoped to (org, namespace,
agent, thread_id). One row per message; readers fetch a thread by id
or page through recent threads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict, cast

from pydantic import BaseModel, ConfigDict

from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.conversations import ConversationRole


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
    ch: _CHClient | None = None,
) -> None:
    """Append one message. ts defaults to now() at the CH side."""
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
    }
    if msg.ts is not None:
        row["ts"] = msg.ts
    await ch.insert(
        "loom.conversations",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def message_exists(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    msg_id: str,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Whether a (thread_id, msg_id) row already exists. Makes
    `conversation.append` idempotent: loom.conversations is a plain MergeTree
    (append-only, no engine dedup), so a caller re-logging the same turn — a
    chat client self-logging via the MCP primer, a gateway retry — would
    otherwise double-write. Known limit: read-before-write, so two *concurrent*
    identical appends can still both land; fine for the sequential per-turn
    logging path, and explicit unique msg_ids avoid it entirely."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT 1 AS ok FROM loom.conversations
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String} AND thread_id = {tid:String}
          AND msg_id = {mid:String}
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
    return bool(row and row.get("ok"))


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

    Turn it on per namespace with `conversation.set_logging` or the Sessions
    "Recording" toggle in the dashboard.
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
        settings={"async_insert": 0, "wait_for_async_insert": 1},
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
    await ch.insert(
        "loom.conversation_embeddings",
        [cast(dict[str, Any], row)],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


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
    holds (see `_tombstone_embeddings`).

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
    await _tombstone_embeddings(org, namespace, agent, thread_id, msg_ids=None, when=when, ch=ch)
    # One per turn (FINAL, however many embedding models hold it), earlier calls'
    # tombstones included, as the messages count is.
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

    # 3. Thread row.
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
    thread_id: str | None = None,
    role: str | None = None,
    run_id: str | None = None,
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
    agent: str | None = None,
    count_turns: bool = True,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The namespace's threads with a live message in the window, most recent
    first: message count, first/last seen and the latest message's agent
    (`last_agent`), role and 240-character excerpt. `count_turns`: the message
    count is the thread's distinct turns, as an opened thread shows; off, the
    window's raw row count (for a caller that does not read the count)."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "wd": window_days, "lim": limit}
    # The aggregate is aliased `last_agent`, not `agent`: the analyzer would resolve a bare
    # `agent` in WHERE to the alias and reject the statement (code 184) under an agent filter.
    agent_clause = ""
    if agent:
        agent_clause = " AND agent = {agent:String}"
        params["agent"] = agent
    rows = await ch.query_all(
        f"""
        SELECT
            thread_id,
            argMax(agent, ts)                                AS last_agent,
            count()                                          AS msg_count,
            min(ts)                                          AS first_seen,
            max(ts)                                          AS last_seen,
            argMax(toString(role), ts)                       AS last_role,
            argMax(substring(content, 1, 240), ts)           AS last_content_excerpt
        FROM loom.conversations
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND ts > now() - toIntervalDay({{wd:UInt32}})
          AND tombstoned_at IS NULL
          {agent_clause}
        GROUP BY thread_id
        ORDER BY last_seen DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    if not rows or not count_turns:
        return rows
    # The count an opened thread shows (each thread whole, one per turn however many agents
    # logged it), for the page's threads only: an exact distinct count over the whole
    # window would hold every turn's key in memory to rank threads it then drops.
    per_thread = "count()" if agent else f"uniqExact({TURN})"
    counted = await ch.query_all(
        f"""
        SELECT thread_id, {per_thread} AS n
        FROM loom.conversations
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND thread_id IN {{tids:Array(String)}}
          AND tombstoned_at IS NULL
          {agent_clause}
        GROUP BY thread_id
        """,
        params={**params, "tids": [r["thread_id"] for r in rows]},
    )
    turns = {r["thread_id"]: r["n"] for r in counted}
    return [{**r, "msg_count": turns.get(r["thread_id"], r["msg_count"])} for r in rows]


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
    """Every live message of one thread, oldest first. loom.conversations is a
    plain MergeTree: FINAL is illegal there (code 181), and
    conversation.append's existence check keeps msg_ids unique."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT msg_id, role, content, ts, metadata, user_id, run_id
        FROM loom.conversations
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND thread_id = {tid:String}
          AND tombstoned_at IS NULL
        ORDER BY ts ASC, msg_id ASC
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
    conversation.search stops returning them too. Synchronous mutations, so the
    next read sees a coherent thread. The embeddings go first: a retry after a
    failure still finds the messages live and repeats both."""
    ch = ch or _default_client()
    await _tombstone_embeddings(org, namespace, agent, thread_id, msg_ids=msg_ids, when=when, ch=ch)
    await ch.execute(
        """
        ALTER TABLE loom.conversations
        UPDATE tombstoned_at = {now:DateTime}
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND thread_id = {tid:String}
          AND msg_id IN {ids:Array(String)}
        SETTINGS mutations_sync = 2
        """,
        params={
            "now": when,
            "org": org,
            "ns": namespace,
            "agent": agent,
            "tid": thread_id,
            "ids": msg_ids,
        },
    )


# Characters of a turn its embedding is built from, which the embedding row keeps.
EMBEDDED_EXCERPT_CHARS = 2048

# Turns per embeddings tombstone mutation. A whole-thread delete has no cap on its
# turns, so its list goes in chunks, one synchronous mutation each: a thread of
# 10,000 turns runs ten. A compaction's window is bounded by its prompt.
_TOMBSTONE_CHUNK = 1000


# Every turn an agent logged in a thread (live or tombstoned), and its embedding rows
# that no message row backs (the conversation retention TTL expires message rows only).
_THREAD_TURNS = """(msg_id IN (
            SELECT msg_id FROM loom.conversations
            WHERE org = {org:String} AND namespace = {ns:String}
              AND agent = {agent:String} AND thread_id = {tid:String}
          )
          OR (agent = {agent:String} AND msg_id NOT IN (
            SELECT msg_id FROM loom.conversations
            WHERE org = {org:String} AND namespace = {ns:String}
              AND thread_id = {tid:String}
          )))"""


async def _tombstone_embeddings(
    org: str,
    namespace: str,
    agent: str,
    thread_id: str,
    *,
    msg_ids: list[str] | None,
    when: datetime,
    ch: _CHClient,
) -> None:
    """Tombstone the embeddings of `agent`'s turns in one thread.

    The turns are `msg_ids`, or every turn the agent logged in the thread
    (`_THREAD_TURNS`). The embeddings key has no agent, so a turn several agents
    logged is one row: it goes by turn, and stays only while another agent holds a
    live copy with the row's text (a msg_id is unique per agent only, so another
    agent's different turn under the same id does not keep it).
    The turns are read first, with FINAL so a row's agent and tombstone are those
    search and the delete count see, and the mutation names them, so it evaluates no
    subquery (a replicated table refuses one)."""
    # Named turns are the agent's own (compaction reads them from its live turns).
    turns = "msg_id IN {ids:Array(String)}" if msg_ids is not None else _THREAD_TURNS
    params: dict[str, Any] = {"org": org, "ns": namespace, "agent": agent, "tid": thread_id}
    if msg_ids is not None:
        params["ids"] = msg_ids
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT msg_id FROM loom.conversation_embeddings FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND thread_id = {{tid:String}} AND tombstoned_at IS NULL
          AND {turns}
          AND (msg_id, content_excerpt) NOT IN (
            SELECT msg_id, substringUTF8(content, 1, {{embedded:UInt32}})
            FROM loom.conversations
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND thread_id = {{tid:String}} AND agent != {{agent:String}}
              AND tombstoned_at IS NULL
          )
        """,
        params={**params, "embedded": EMBEDDED_EXCERPT_CHARS},
    )
    due = [str(r["msg_id"]) for r in rows]
    for i in range(0, len(due), _TOMBSTONE_CHUNK):
        await ch.execute(
            """
            ALTER TABLE loom.conversation_embeddings
            UPDATE tombstoned_at = {now:DateTime}
            WHERE org = {org:String} AND namespace = {ns:String}
              AND thread_id = {tid:String} AND tombstoned_at IS NULL
              AND msg_id IN {due:Array(String)}
            SETTINGS mutations_sync = 2
            """,
            params={
                "now": when,
                "org": org,
                "ns": namespace,
                "tid": thread_id,
                "due": due[i : i + _TOMBSTONE_CHUNK],
            },
        )


async def titler_candidates(
    *, window_hours: int, min_turns: int, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Recent threads with at least `min_turns` live messages in namespaces that
    record conversations, untitled and not deleted, most recent first."""
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
          -- Titled and deleted threads are not candidates: skipped after
          -- the LIMIT, they filled it every pass and older untitled
          -- threads were never reached.
          AND (c.org, c.namespace, c.thread_id) NOT IN (
              SELECT org, namespace, thread_id FROM loom.conversation_threads FINAL
              WHERE tombstoned_at IS NOT NULL OR title != ''
          )
        GROUP BY c.org, c.namespace, c.thread_id
        HAVING n >= {mt:UInt32}
        ORDER BY max(c.ts) DESC
        LIMIT {lim:UInt32}
        """,
        params={"wh": window_hours, "mt": min_turns, "lim": limit},
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
    org: str, namespace: str, thread_id: str, *, limit: int = 8, ch: _CHClient | None = None
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
