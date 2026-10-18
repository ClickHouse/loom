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


@pytest.mark.asyncio
async def test_a_peer_row_written_after_the_read_is_not_tombstoned(ch, scope) -> None:
    """The mutation names the row version the read chose: a peer's newer row under the
    same msg_id, written between the read and the mutation, stays live."""
    await _message(ch, scope, "a", "1", "mine", T0)
    await _embedding(ch, scope, "a", "1", "mine", T0)

    class _Interleaving:
        """Passes through, and lands a peer's turn right after the read."""

        def __init__(self) -> None:
            self.done = False

        async def query_all(self, sql: str, params: Any = None) -> Any:
            rows = await ch.query_all(sql, params=params)
            if not self.done and "FROM loom.conversation_embeddings FINAL" in sql:
                self.done = True
                later = T0 + timedelta(seconds=5)
                await _message(ch, scope, "b", "1", "theirs", later)
                await _embedding(ch, scope, "b", "1", "theirs", later)
            return rows

        async def query_one(self, sql: str, params: Any = None) -> Any:
            return await ch.query_one(sql, params=params)

        async def execute(self, sql: str, params: Any = None, **kw: Any) -> Any:
            return await ch.execute(sql, params, **kw)

    await repo.tombstone_thread(
        scope["org"],
        scope["ns"],
        "a",
        scope["tid"],
        when=datetime.now(UTC).replace(tzinfo=None),
        ch=_Interleaving(),
    )
    assert await _rows(ch, scope) == [("1", "theirs", True)]


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
async def test_a_delete_of_long_msg_ids_binds_them_in_slices(ch, scope) -> None:
    """The same 300 live turns, deleted: the row-version list goes in byte-bounded
    slices and every embedding is tombstoned."""
    await _long_turns(ch, scope)
    now = datetime.now(UTC).replace(tzinfo=None)
    out = await repo.tombstone_thread(scope["org"], scope["ns"], "a", scope["tid"], when=now, ch=ch)
    assert out["loom.conversation_embeddings"] == 300
    assert {live for _, _, live in await _rows(ch, scope)} == {False}
