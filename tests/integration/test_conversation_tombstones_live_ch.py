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


async def _message(ch: Any, s: dict[str, str], agent: str, mid: str, text: str, ts: datetime):
    await repo.append(
        repo.MessageInsert(
            org=s["org"],
            namespace=s["ns"],
            agent=agent,
            thread_id=s["tid"],
            msg_id=mid,
            role="user",
            content=text,
            ts=ts,
        ),
        ch=ch,
    )


async def _embedding(ch: Any, s: dict[str, str], agent: str, mid: str, text: str, ts: datetime):
    await repo.insert_embedding(
        {
            "org": s["org"],
            "namespace": s["ns"],
            "agent": agent,
            "thread_id": s["tid"],
            "msg_id": mid,
            "role": "user",
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


class _AfterFirstTombstone(_PassThrough):
    """Runs `then` once the first embeddings tombstone has landed."""

    def __init__(self, ch: Any, then: Any) -> None:
        super().__init__(ch)
        self.then = then

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> Any:
        out = await super().execute(sql, params, **kw)
        if self.then and "INSERT INTO loom.conversation_embeddings" in sql:
            then, self.then = self.then, None
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
        scope["org"], scope["ns"], "a", scope["tid"], when=when, ch=_AfterFirstTombstone(ch, peer)
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
        scope["org"], scope["ns"], "a", scope["tid"], when=when, ch=_AfterFirstTombstone(ch, append)
    )
    assert out["loom.conversations"] == 2  # the mid-delete turn's message was caught too
    assert out["loom.conversation_embeddings"] == 2
    assert await _rows(ch, scope) == [("1", "first", False), ("2", "second", False)]


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
        ch=_AfterFirstTombstone(ch, embed),
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
    rewritten with that copy's agent, run and user, so this agent's own search drops
    it and the peer's searches and erasure find it."""
    for agent in ("b", "a"):
        await repo.append(
            repo.MessageInsert(
                org=scope["org"],
                namespace=scope["ns"],
                agent=agent,
                thread_id=scope["tid"],
                msg_id="1",
                role="user",
                content="shared",
                user_id=f"user-{agent}",
                run_id=f"run-{agent}",
                ts=T0,
            ),
            ch=ch,
        )
    await _embedding(ch, scope, "a", "1", "shared", T0)
    for tombstone in (
        lambda: repo.tombstone_messages(
            scope["org"], scope["ns"], "a", scope["tid"], ["1"], when=T0, ch=ch
        ),
        lambda: repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=T0, ch=ch),
    ):
        await tombstone()
        rows = await ch.query_all(
            "SELECT agent, run_id, user_id, tombstoned_at IS NULL AS live "
            "FROM loom.conversation_embeddings FINAL WHERE org = {org:String} "
            "AND namespace = {ns:String} AND thread_id = {tid:String}",
            params={"org": scope["org"], "ns": scope["ns"], "tid": scope["tid"]},
        )
        assert [(r["agent"], r["run_id"], r["user_id"], bool(r["live"])) for r in rows] == [
            ("b", "run-b", "user-b", True)
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [0, 1, 2, 3, 4])
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
