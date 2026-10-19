"""The conversation-embeddings tombstone against live ClickHouse: the row versions it
names, the rows an agent owns, and long ids."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.knowledge.store import conversations as repo
from tests.integration._resources import unique_org

T0 = datetime(2026, 9, 1, 12, 0, 0)
MODEL = "test-model"


@pytest.fixture(scope="module")
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
def scope() -> dict[str, str]:
    tag = uuid.uuid4().hex[:10]
    return {"org": unique_org("conv-tomb"), "ns": f"ns-{tag}", "tid": f"t-{tag}"}


async def _message(
    ch: Any,
    s: dict[str, str],
    agent: str,
    mid: str,
    text: str,
    ts: datetime,
    role: str = "user",
    metadata: str = "{}",
):
    await repo.append(
        repo.MessageInsert(
            org=s["org"],
            namespace=s["ns"],
            agent=agent,
            thread_id=s["tid"],
            msg_id=mid,
            role=role,
            content=text,
            metadata=metadata,
            ts=ts,
        ),
        ch=ch,
    )


async def _embedding(
    ch: Any, s: dict[str, str], agent: str, mid: str, text: str, ts: datetime, role: str = "user"
):
    await repo.insert_embedding(
        {
            "org": s["org"],
            "namespace": s["ns"],
            "agent": agent,
            "thread_id": s["tid"],
            "msg_id": mid,
            "role": role,
            "content_excerpt": text,
            "embedding_model": MODEL,
            "embedding_dim": 0,
            "embedding": [],
            "user_id": "-",
            "run_id": "-",
            "embed_attempts": 0,
            "embed_last_error": "",
            "embed_failed_at": None,
            "ts": ts,
        },
        ch=ch,
    )


async def _rows(ch: Any, s: dict[str, str]) -> list[tuple[str, str, bool]]:
    rows = await ch.query_all(
        "SELECT msg_id, content_excerpt, tombstoned_at IS NULL AS live "
        "FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String} "
        "ORDER BY msg_id",
        params={"org": s["org"], "ns": s["ns"], "tid": s["tid"]},
    )
    return [(r["msg_id"], r["content_excerpt"], bool(r["live"])) for r in rows]


class _PassThrough:
    """Passes every statement through to ClickHouse; a test overrides `execute`."""

    def __init__(self, ch: Any) -> None:
        self.ch = ch

    async def query_all(self, sql: str, params: Any = None) -> Any:
        return await self.ch.query_all(sql, params=params)

    async def query_one(self, sql: str, params: Any = None) -> Any:
        return await self.ch.query_one(sql, params=params)

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> Any:
        return await self.ch.execute(sql, params, **kw)


class _AfterFirstPass(_PassThrough):
    """Runs `then` once the first embeddings pass is done: before the messages
    mutation, or after it with `after_mutation`."""

    def __init__(self, ch: Any, then: Any, *, after_mutation: bool = False) -> None:
        super().__init__(ch)
        self.then, self.after_mutation = then, after_mutation

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> Any:
        if not (self.then and "ALTER TABLE loom.conversations\n" in sql):
            return await super().execute(sql, params, **kw)
        then, self.then = self.then, None
        if not self.after_mutation:
            await then()
        out = await super().execute(sql, params, **kw)
        if self.after_mutation:
            await then()
        return out


class _FailOnce(_PassThrough):
    """Fails the `fail_at`-th write statement (0-based)."""

    def __init__(self, ch: Any, fail_at: int) -> None:
        super().__init__(ch)
        self.fail_at, self.writes = fail_at, 0

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> Any:
        self.writes += 1
        if self.writes - 1 == self.fail_at:
            raise RuntimeError("statement failed")
        return await super().execute(sql, params, **kw)


@pytest.mark.asyncio
async def test_a_peer_row_written_after_the_tombstone_stays_live(ch, scope) -> None:
    """The tombstone is a version row at the old row's `ts`: a peer's row under the
    same msg_id, written after it with a newer `ts`, wins and stays live."""
    await _message(ch, scope, "a", "1", "mine", T0)
    await _embedding(ch, scope, "a", "1", "mine", T0)

    async def peer() -> None:
        later = T0 + timedelta(seconds=5)
        await _message(ch, scope, "b", "1", "theirs", later)
        await _embedding(ch, scope, "b", "1", "theirs", later)

    when = datetime.now(UTC).replace(tzinfo=None)
    await repo.tombstone_thread(
        scope["org"], scope["ns"], "a", scope["tid"], when=when, ch=_AfterFirstPass(ch, peer)
    )
    assert await _rows(ch, scope) == [("1", "theirs", True)]


@pytest.mark.asyncio
async def test_a_turn_appended_during_the_delete_leaves_search(ch, scope) -> None:
    """The agent's own turn whose message lands before the messages mutation and whose
    embedding lands after the first embeddings pass is tombstoned by the second."""
    await _message(ch, scope, "a", "1", "first", T0)
    await _embedding(ch, scope, "a", "1", "first", T0)

    async def append() -> None:
        later = T0 + timedelta(seconds=5)
        await _message(ch, scope, "a", "2", "second", later)
        await _embedding(ch, scope, "a", "2", "second", later)

    when = datetime.now(UTC).replace(tzinfo=None)
    out = await repo.tombstone_thread(
        scope["org"], scope["ns"], "a", scope["tid"], when=when, ch=_AfterFirstPass(ch, append)
    )
    assert out["loom.conversations"] == 2  # the mid-delete turn's message was caught too
    assert out["loom.conversation_embeddings"] == 2
    assert await _rows(ch, scope) == [("1", "first", False), ("2", "second", False)]


@pytest.mark.asyncio
async def test_a_turn_appended_after_the_messages_mutation_stays_out_of_search(ch, scope) -> None:
    """The agent's own turn whose message lands after the delete's messages mutation
    stays live, but its embedding, landing before the last pass, is tombstoned: a turn
    of a deleted thread does not come back through search."""
    await _message(ch, scope, "a", "1", "first", T0)

    async def append() -> None:
        later = T0 + timedelta(seconds=5)
        await _message(ch, scope, "a", "2", "second", later)
        await _embedding(ch, scope, "a", "2", "second", later)

    when = datetime.now(UTC).replace(tzinfo=None)
    await repo.tombstone_thread(
        scope["org"],
        scope["ns"],
        "a",
        scope["tid"],
        when=when,
        ch=_AfterFirstPass(ch, append, after_mutation=True),
    )
    live = await repo.live_messages(scope["org"], scope["ns"], "a", scope["tid"], ch=ch)
    assert [m["msg_id"] for m in live] == ["2"]
    assert await _rows(ch, scope) == [("2", "second", False)]


@pytest.mark.asyncio
async def test_a_turn_embedded_during_compaction_leaves_search(ch, scope) -> None:
    """A compacted turn whose embedding lands after compaction's first embeddings pass
    is tombstoned by the pass after the messages mutation."""
    await _message(ch, scope, "a", "1", "slow embed", T0)

    async def embed() -> None:
        await _embedding(ch, scope, "a", "1", "slow embed", T0)

    when = datetime.now(UTC).replace(tzinfo=None)
    await repo.tombstone_messages(
        scope["org"],
        scope["ns"],
        "a",
        scope["tid"],
        ["1"],
        when=when,
        ch=_AfterFirstPass(ch, embed),
    )
    assert await _rows(ch, scope) == [("1", "slow embed", False)]


@pytest.mark.asyncio
async def test_an_orphan_embedding_goes_although_a_peer_reused_its_id(ch, scope) -> None:
    """This agent's embedding whose message row is gone is its own, whatever another
    agent logged under the same msg_id: the delete tombstones it."""
    await _embedding(ch, scope, "a", "1", "mine", T0)
    await _message(ch, scope, "b", "1", "an unrelated tool turn", T0 - timedelta(seconds=5))

    await repo.tombstone_thread(
        scope["org"],
        scope["ns"],
        "a",
        scope["tid"],
        when=datetime.now(UTC).replace(tzinfo=None),
        ch=ch,
    )
    assert await _rows(ch, scope) == [("1", "mine", False)]


def _stored(s: dict[str, str], mid: str, text: str) -> tuple[Any, Any]:
    """A message and its embed result, as append and compact hand them to
    `_store_embedding`."""
    from server.knowledge.app.conversations import EmbeddedTurn

    msg = repo.MessageInsert(
        org=s["org"],
        namespace=s["ns"],
        agent="a",
        thread_id=s["tid"],
        msg_id=mid,
        role="user",
        content=text,
        ts=T0,
    )
    return msg, EmbeddedTurn(text, [], 0, MODEL, "")


@pytest.mark.asyncio
async def test_an_embedding_stored_after_the_delete_is_tombstoned_by_its_writer(ch, scope) -> None:
    """The message lands before the delete and its embedding after the delete's last
    pass: the writer's re-check in `_store_embedding` tombstones the embedding."""
    from server.knowledge.app.conversations import _store_embedding

    await _message(ch, scope, "a", "1", "late", T0)
    now = datetime.now(UTC).replace(tzinfo=None)
    await repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=now, ch=ch)
    msg, embedded = _stored(scope, "1", "late")
    await _store_embedding(msg, embedded)
    assert await _rows(ch, scope) == [("1", "late", False)]


@pytest.mark.asyncio
async def test_a_live_turn_keeps_its_embedding_after_the_recheck(ch, scope) -> None:
    from server.knowledge.app.conversations import _store_embedding

    await _message(ch, scope, "a", "1", "kept", T0)
    msg, embedded = _stored(scope, "1", "kept")
    await _store_embedding(msg, embedded)
    assert await _rows(ch, scope) == [("1", "kept", True)]


@pytest.mark.asyncio
async def test_a_shared_row_with_a_short_excerpt_survives_the_delete(ch, scope) -> None:
    """A row embedded when excerpts held 240 characters still matches a peer's live
    copy of a longer turn: the shared row stays."""
    text = "deploy " * 100
    await _message(ch, scope, "a", "1", text, T0)
    await _message(ch, scope, "b", "1", text, T0)
    await _embedding(ch, scope, "b", "1", text[:240], T0)
    now = datetime.now(UTC).replace(tzinfo=None)
    await repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=now, ch=ch)
    assert await _rows(ch, scope) == [("1", text[:240], True)]


@pytest.mark.asyncio
async def test_a_peer_turn_sharing_only_a_prefix_does_not_keep_the_row(ch, scope) -> None:
    """A peer's different turn under the same msg_id that shares the first 240
    characters does not match a row holding the full excerpt: the delete takes it."""
    head = "deploy " * 40
    mine, theirs = head + "to friday", head + "to monday"
    await _message(ch, scope, "a", "1", mine, T0)
    await _message(ch, scope, "b", "1", theirs, T0)
    await _embedding(ch, scope, "a", "1", mine, T0)
    now = datetime.now(UTC).replace(tzinfo=None)
    await repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=now, ch=ch)
    assert await _rows(ch, scope) == [("1", mine, False)]


@pytest.mark.asyncio
async def test_a_kept_row_passes_to_the_agent_that_still_holds_the_turn(ch, scope) -> None:
    """A shared row this agent wrote last stays live for the peer's copy, and is
    rewritten with that copy's agent, role, run and user, so this agent's own search
    drops it and the peer's searches (a role filter included) and erasure find it. A
    client-chosen msg_id is unique per agent only, so the two copies can differ in
    role."""
    for agent, role in (("b", "user"), ("a", "assistant")):
        await repo.append(
            repo.MessageInsert(
                org=scope["org"],
                namespace=scope["ns"],
                agent=agent,
                thread_id=scope["tid"],
                msg_id="1",
                role=role,
                content="shared",
                user_id=f"user-{agent}",
                run_id=f"run-{agent}",
                ts=T0,
            ),
            ch=ch,
        )
    await _embedding(ch, scope, "a", "1", "shared", T0, role="assistant")
    for tombstone in (
        lambda: repo.tombstone_messages(
            scope["org"], scope["ns"], "a", scope["tid"], ["1"], when=T0, ch=ch
        ),
        lambda: repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=T0, ch=ch),
    ):
        await tombstone()
        rows = await ch.query_all(
            "SELECT agent, role, run_id, user_id, tombstoned_at IS NULL AS live "
            "FROM loom.conversation_embeddings FINAL WHERE org = {org:String} "
            "AND namespace = {ns:String} AND thread_id = {tid:String}",
            params={"org": scope["org"], "ns": scope["ns"], "tid": scope["tid"]},
        )
        assert [
            (r["agent"], r["role"], r["run_id"], r["user_id"], bool(r["live"])) for r in rows
        ] == [("b", "user", "run-b", "user-b", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [0, 1, 2])
async def test_a_failed_compaction_step_never_leaves_a_hidden_turn_searchable(
    ch, scope, fail_at
) -> None:
    """Whichever statement of the compaction tombstone fails, no turn is left with its
    message tombstoned and its embedding live, and a retry finishes the job."""
    await _message(ch, scope, "a", "1", "retry me", T0)
    await _embedding(ch, scope, "a", "1", "retry me", T0)

    when = datetime.now(UTC).replace(tzinfo=None)
    args = (scope["org"], scope["ns"], "a", scope["tid"], ["1"])
    with pytest.raises(RuntimeError):
        await repo.tombstone_messages(*args, when=when, ch=_FailOnce(ch, fail_at))
    live = await repo.live_messages(scope["org"], scope["ns"], "a", scope["tid"], ch=ch)
    [(_, _, embedding_live)] = await _rows(ch, scope)
    assert live or not embedding_live  # a hidden turn is never searchable

    await repo.tombstone_messages(*args, when=when, ch=ch)
    assert await repo.live_messages(scope["org"], scope["ns"], "a", scope["tid"], ch=ch) == []
    assert await _rows(ch, scope) == [("1", "retry me", False)]


@pytest.mark.asyncio
async def test_a_retried_compaction_finishes_a_failed_last_pass(ch, scope) -> None:
    """The turn's embedding lands after compaction's first embeddings pass and the last
    pass fails, so the turn is hidden and its embedding live. The retry's window no
    longer holds that turn, but the op passes over what each live summary replaced
    and tombstones it."""
    from server.knowledge.api.models_conversations import ConversationCompactInput
    from server.knowledge.app.conversations import handle_conversation_compact

    await _message(ch, scope, "a", "1", "retry me", T0)
    await _message(
        ch, scope, "a", "s", "summary", T0, role="system", metadata='{"compacted_from": ["1"]}'
    )

    class _EmbedThenFail(_FailOnce):
        async def execute(self, sql: str, params: Any = None, **kw: Any) -> Any:
            if self.writes == 1:  # the messages mutation, after the first pass
                await _embedding(ch, scope, "a", "1", "retry me", T0)
            return await super().execute(sql, params, **kw)

    args = (scope["org"], scope["ns"], "a", scope["tid"], ["1"])
    with pytest.raises(RuntimeError):
        await repo.tombstone_messages(*args, when=T0, ch=_EmbedThenFail(ch, 2))
    assert await _rows(ch, scope) == [("1", "retry me", True)]

    out = await handle_conversation_compact(
        ConversationCompactInput(
            org=scope["org"], namespace=scope["ns"], agent="a", thread_id=scope["tid"]
        )
    )
    assert out.compacted is False
    assert await _rows(ch, scope) == [("1", "retry me", False)]


@pytest.mark.asyncio
async def test_a_compaction_that_failed_before_the_mutation_keeps_live_turns_searchable(
    ch, scope
) -> None:
    """A compaction appended its summary and failed before tombstoning anything: the
    turns it names are still live, and the next call, compacting nothing, leaves their
    embeddings live."""
    from server.knowledge.api.models_conversations import ConversationCompactInput
    from server.knowledge.app.conversations import handle_conversation_compact

    await _message(ch, scope, "a", "1", "still here", T0)
    await _embedding(ch, scope, "a", "1", "still here", T0)
    await _message(
        ch, scope, "a", "s", "summary", T0, role="system", metadata='{"compacted_from": ["1"]}'
    )
    out = await handle_conversation_compact(
        ConversationCompactInput(
            org=scope["org"], namespace=scope["ns"], agent="a", thread_id=scope["tid"]
        )
    )
    assert out.compacted is False
    assert await _rows(ch, scope) == [("1", "still here", True)]


class _LateReconcile(_PassThrough):
    """Runs the first embeddings reconcile as a read, then `meanwhile`, then the write
    of what it read: a reconcile whose rows land after another agent's whole delete."""

    def __init__(self, ch: Any, meanwhile: Any) -> None:
        super().__init__(ch)
        self.meanwhile: Any = meanwhile

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> Any:
        if self.meanwhile is None or "p.holder.1" not in sql:
            return await super().execute(sql, params, **kw)
        meanwhile, self.meanwhile = self.meanwhile, None
        select = sql.split("INSERT INTO loom.conversation_embeddings", 1)[1]
        rows = await self.ch.query_all(select, params=params)
        await meanwhile()
        if rows:
            await self.ch.insert("loom.conversation_embeddings", rows)
        return None


@pytest.mark.asyncio
async def test_two_agents_deleting_a_shared_turn_at_once_both_remove_it(ch, scope) -> None:
    """A's first reconcile reads B's copy as live, B's whole delete runs (keeping the
    row for A's still-live copy), then A's reconcile lands it under B: A's next read
    finds B holds no live copy and tombstones it, so the turn leaves search."""
    await _message(ch, scope, "a", "1", "shared", T0)
    await _message(ch, scope, "b", "1", "shared", T0)
    await _embedding(ch, scope, "a", "1", "shared", T0)
    when = datetime.now(UTC).replace(tzinfo=None)

    async def b_deletes() -> None:
        await repo.tombstone_thread(scope["org"], scope["ns"], "b", scope["tid"], when=when, ch=ch)

    await repo.tombstone_thread(
        scope["org"], scope["ns"], "a", scope["tid"], when=when, ch=_LateReconcile(ch, b_deletes)
    )
    assert await _rows(ch, scope) == [("1", "shared", False)]


class _PingPong(_PassThrough):
    """After the first reconcile lands, runs `after_first`; runs the second reconcile
    as a read, then `meanwhile`, then the write of what it read."""

    def __init__(self, ch: Any, after_first: Any, meanwhile: Any) -> None:
        super().__init__(ch)
        self.after_first, self.meanwhile, self.handoffs = after_first, meanwhile, 0

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> Any:
        if "p.holder.1" not in sql:
            return await super().execute(sql, params, **kw)
        self.handoffs += 1
        if self.handoffs == 1:
            out = await super().execute(sql, params, **kw)
            await self.after_first()
            return out
        if self.handoffs == 2:
            select = sql.split("INSERT INTO loom.conversation_embeddings", 1)[1]
            rows = await self.ch.query_all(select, params=params)
            await self.meanwhile()
            if rows:
                await self.ch.insert("loom.conversation_embeddings", rows)
            return None
        return await super().execute(sql, params, **kw)


@pytest.mark.asyncio
async def test_a_late_handoff_after_the_peer_hands_the_row_back_still_ends_tombstoned(
    ch, scope
) -> None:
    """A hands the row to B; B's first pass hands it back (A's copy is still live);
    A's second reconcile reads B's copy as live and lands after B's whole delete. A
    reconcile repeats until a later read finds no unheld row, so the turn both agents
    removed leaves search."""
    await _message(ch, scope, "a", "1", "shared", T0)
    await _message(ch, scope, "b", "1", "shared", T0)
    await _embedding(ch, scope, "a", "1", "shared", T0)
    when = datetime.now(UTC).replace(tzinfo=None)

    async def b_first_pass() -> None:
        await repo.tombstone_embeddings(
            scope["org"], scope["ns"], "b", scope["tid"], msg_ids=None, when=when, ch=ch
        )

    async def b_deletes() -> None:
        await repo.tombstone_thread(scope["org"], scope["ns"], "b", scope["tid"], when=when, ch=ch)

    await repo.tombstone_thread(
        scope["org"],
        scope["ns"],
        "a",
        scope["tid"],
        when=when,
        ch=_PingPong(ch, b_first_pass, b_deletes),
    )
    assert await _rows(ch, scope) == [("1", "shared", False)]


async def _holders(ch: Any, s: dict[str, str]) -> list[tuple[str, bool]]:
    rows = await ch.query_all(
        "SELECT agent, tombstoned_at IS NULL AS live FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String}",
        params={"org": s["org"], "ns": s["ns"], "tid": s["tid"]},
    )
    return [(r["agent"], bool(r["live"])) for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("whole_delete", [True, False])
async def test_a_late_reconcile_to_a_removed_peer_passes_to_the_one_left(
    ch, scope, whole_delete
) -> None:
    """Three agents hold the turn. A's reconcile reads B's copy as live and lands after
    B's whole delete: the row ends with C, the one agent still holding it, not with B.
    A single pass shows the re-read alone catches it, with no later pass to help."""
    for agent in "abc":
        await _message(ch, scope, agent, "1", "shared", T0 + timedelta(seconds="cba".index(agent)))
    await _embedding(ch, scope, "a", "1", "shared", T0)
    when = datetime.now(UTC).replace(tzinfo=None)

    async def b_deletes() -> None:
        await repo.tombstone_thread(scope["org"], scope["ns"], "b", scope["tid"], when=when, ch=ch)

    late = _LateReconcile(ch, b_deletes)
    if whole_delete:
        await repo.tombstone_thread(
            scope["org"], scope["ns"], "a", scope["tid"], when=when, ch=late
        )
    else:
        await repo.tombstone_embeddings(
            scope["org"], scope["ns"], "a", scope["tid"], msg_ids=None, when=when, ch=late
        )
    assert await _holders(ch, scope) == [("c", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["tool", "system"])
@pytest.mark.parametrize("op", ["delete", "compact"])
async def test_a_transcript_only_peer_turn_does_not_keep_the_row(ch, scope, role, op) -> None:
    """A peer's tool or system turn with the same id and text is not indexed, so it
    does not keep this agent's row: the delete or compaction tombstones it."""
    await _message(ch, scope, "a", "1", "same text", T0, role="assistant")
    await _message(ch, scope, "b", "1", "same text", T0, role=role)
    await _embedding(ch, scope, "a", "1", "same text", T0, role="assistant")
    if op == "delete":
        await repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=T0, ch=ch)
    else:
        await repo.tombstone_messages(
            scope["org"], scope["ns"], "a", scope["tid"], ["1"], when=T0, ch=ch
        )
    assert await _holders(ch, scope) == [("a", False)]


@pytest.mark.asyncio
async def test_a_peer_compaction_summary_keeps_the_row(ch, scope) -> None:
    """A compaction summary is indexed although its role is system: a peer's copy of
    one keeps the row and takes it over."""
    await _message(ch, scope, "a", "1", "summary", T0, role="system")
    await _message(
        ch, scope, "b", "1", "summary", T0, role="system", metadata='{"compacted_from": ["0"]}'
    )
    await _embedding(ch, scope, "a", "1", "summary", T0, role="system")
    await repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=T0, ch=ch)
    assert await _holders(ch, scope) == [("b", True)]


_LONG_IDS = [f"{i:04d}" + "x" * 508 for i in range(300)]  # 512-character ids


async def _long_turns(ch: Any, s: dict[str, str]) -> None:
    for i, mid in enumerate(_LONG_IDS):
        ts = T0 + timedelta(seconds=i)
        await _message(ch, s, "a", mid, f"turn {i}", ts)
        await _embedding(ch, s, "a", mid, f"turn {i}", ts)


@pytest.mark.asyncio
async def test_a_compaction_of_long_msg_ids_binds_them_in_slices(ch, scope) -> None:
    """300 turns with 512-character ids, one unsliced parameter past the server's
    form-field cap: compaction tombstones every message and embedding."""
    await _long_turns(ch, scope)
    now = datetime.now(UTC).replace(tzinfo=None)
    await repo.tombstone_messages(
        scope["org"], scope["ns"], "a", scope["tid"], _LONG_IDS, when=now, ch=ch
    )
    assert {live for _, _, live in await _rows(ch, scope)} == {False}
    left = await ch.query_one(
        "SELECT count() AS n FROM loom.conversations WHERE org = {org:String} "
        "AND namespace = {ns:String} AND thread_id = {tid:String} AND tombstoned_at IS NULL",
        params={"org": scope["org"], "ns": scope["ns"], "tid": scope["tid"]},
    )
    assert left["n"] == 0


@pytest.mark.asyncio
async def test_a_delete_of_long_msg_ids_tombstones_every_turn(ch, scope) -> None:
    """The same 300 live turns, deleted as a whole thread: every embedding is
    tombstoned and counted."""
    await _long_turns(ch, scope)
    now = datetime.now(UTC).replace(tzinfo=None)
    out = await repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=now, ch=ch)
    assert out["loom.conversation_embeddings"] == 300
    assert {live for _, _, live in await _rows(ch, scope)} == {False}
