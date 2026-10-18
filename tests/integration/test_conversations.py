"""Conversations plane — append, list, search."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


async def _admin_post(mint, agent, control_plane_url: str, op: str, body: dict) -> dict:
    """POST `op` with an admin token for the agent's org (`mint` is the
    `mint_test_token` fixture): namespace settings and conversation.delete need one,
    and the `agent` fixture is writer-scoped on purpose."""
    import httpx

    tok = mint(org=agent.org, roles=["admin"], subject="test-suite-admin")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30.0) as c:
        r = await c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {tok}"})
        r.raise_for_status()
        return r.json()


@pytest.fixture(autouse=True)
async def _recording_on(agent, control_plane_url: str, mint_test_token):
    """Opt the test namespace into transcript recording.

    Conversation recording is opt-in per namespace and the
    `agent` fixture mints a fresh, never-registered namespace, so the gate in
    `conversation_repo.namespace_logging_enabled` fails closed and every append
    below would be an accepted no-op. Declaring the opt-in here is what a real
    namespace owner does from the dashboard's Conversations page.

    Deliberately not folded into the `agent` fixture: that would silently turn
    recording on for every integration test, which is the opposite of the
    default, and would hide a regression in the gate itself.

    Needs its own ADMIN token — `conversation.set_logging` is a namespace
    setting, and `agent` is writer-scoped on purpose.
    """
    await _admin_post(
        mint_test_token,
        agent,
        control_plane_url,
        "conversation.set_logging",
        {"org": agent.org, "namespace": agent.namespace, "enabled": True},
    )


@pytest.mark.asyncio
async def test_append_then_list_chronological(agent) -> None:
    """Three appends; list returns them oldest→newest with metadata intact."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    msgs = [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]
    msg_ids = [uuid.uuid4().hex for _ in msgs]
    for mid, (role, content), i in zip(msg_ids, msgs, range(len(msgs)), strict=False):
        r = await agent.conversations.append(
            tid,
            mid,
            role,
            content,
            tokens=10 + i,
            metadata={"client": "test"},
        )
        assert r["msg_id"] == mid
        assert r["ts"]

    page = await agent.conversations.list(tid, limit=10)
    assert page["next_cursor"] is None
    rows = page["rows"]
    assert len(rows) == 3
    for row, (role, content) in zip(rows, msgs, strict=False):
        assert row["role"] == role
        assert row["content"] == content
        assert row["metadata"] == {"client": "test"}


@pytest.mark.asyncio
async def test_search_semantic_returns_relevant(agent) -> None:
    """search() ranks semantically-similar rows above unrelated ones.

    Search is cosineDistance over OpenAI embeddings, not literal substring,
    so a question about "preferences" can match a statement of one, and
    an unrelated chitchat row stays out of the top hits.
    """
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    await agent.conversations.append(
        tid, uuid.uuid4().hex, "user", "I prefer ClickHouse for analytics"
    )
    await agent.conversations.append(
        tid, uuid.uuid4().hex, "assistant", "Got it; I will use ClickHouse"
    )
    await agent.conversations.append(
        tid, uuid.uuid4().hex, "user", "what is the weather today"
    )  # unrelated

    hits = await agent.conversations.search("ClickHouse analytics preference", top_k=2)
    contents = [h["content_excerpt"] for h in hits["hits"]]
    # Top 2 hits must be the two ClickHouse messages, not the weather one.
    assert "I prefer ClickHouse for analytics" in contents
    assert "Got it; I will use ClickHouse" in contents
    assert "what is the weather today" not in contents


@pytest.mark.asyncio
async def test_search_filters_by_role(agent) -> None:
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    await agent.conversations.append(tid, uuid.uuid4().hex, "user", "marker xyz")
    await agent.conversations.append(tid, uuid.uuid4().hex, "assistant", "marker xyz")

    only_user = await agent.conversations.search("marker", role="user", top_k=10)
    roles = {h["role"] for h in only_user["hits"]}
    assert roles == {"user"}


@pytest.mark.asyncio
async def test_list_pagination(agent) -> None:
    """Append 7 messages with limit=3 — walk via cursor sees all 7 exactly once."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    ids = [uuid.uuid4().hex for _ in range(7)]
    for mid, i in zip(ids, range(7), strict=False):
        await agent.conversations.append(tid, mid, "user", f"msg {i}")

    seen: list[str] = []
    cursor = None
    for _ in range(5):  # safety bound
        page = await agent.conversations.list(tid, limit=3, cursor=cursor)
        seen.extend(r["msg_id"] for r in page["rows"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert seen == ids


@pytest.mark.asyncio
async def test_create_then_append_with_reasoning(agent) -> None:
    """conversation.create attaches title + run_id; append rows carry
    a first-class reasoning column distinct from content."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    run = f"run-{uuid.uuid4().hex[:6]}"

    created = await agent.conversations.create(
        tid,
        title="Sprint planning",
        run_id=run,
        metadata={"sprint": "q3-2026"},
    )
    assert created["thread_id"] == tid

    await agent.conversations.append(
        tid,
        uuid.uuid4().hex,
        "user",
        "Should we ship X?",
        reasoning="Evaluating X against quarterly OKRs",
        run_id=run,
    )

    rows = (await agent.conversations.list(tid, run_id=run, limit=5))["rows"]
    assert len(rows) == 1
    assert rows[0]["content"] == "Should we ship X?"
    assert rows[0]["reasoning"] == "Evaluating X against quarterly OKRs"
    assert rows[0]["run_id"] == run


@pytest.mark.asyncio
async def test_run_id_filter_isolates_messages(agent) -> None:
    """list(run_id=...) and search(run_id=...) only return messages from
    that run."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    run_a = f"runA-{uuid.uuid4().hex[:6]}"
    run_b = f"runB-{uuid.uuid4().hex[:6]}"

    await agent.conversations.append(tid, uuid.uuid4().hex, "user", "in run A", run_id=run_a)
    await agent.conversations.append(tid, uuid.uuid4().hex, "user", "in run B", run_id=run_b)

    only_a = (await agent.conversations.list(tid, run_id=run_a, limit=10))["rows"]
    only_b = (await agent.conversations.list(tid, run_id=run_b, limit=10))["rows"]
    assert [r["content"] for r in only_a] == ["in run A"]
    assert [r["content"] for r in only_b] == ["in run B"]


@pytest.mark.asyncio
async def test_thread_scope_isolation(agent) -> None:
    """A message in thread A doesn't show up in thread B's list."""
    a = f"thr-a-{uuid.uuid4().hex[:6]}"
    b = f"thr-b-{uuid.uuid4().hex[:6]}"
    await agent.conversations.append(a, uuid.uuid4().hex, "user", "in thread A")
    await agent.conversations.append(b, uuid.uuid4().hex, "user", "in thread B")

    rows_a = (await agent.conversations.list(a, limit=10))["rows"]
    rows_b = (await agent.conversations.list(b, limit=10))["rows"]
    assert [r["content"] for r in rows_a] == ["in thread A"]
    assert [r["content"] for r in rows_b] == ["in thread B"]


@pytest.mark.asyncio
async def test_a_compacted_turn_leaves_search(agent, ch_client) -> None:
    """conversation.compact tombstones the summarised turns' embeddings with the turns,
    so search returns only what conversation.list still shows."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:10]
    old = [uuid.uuid4().hex for _ in range(4)]
    for i, mid in enumerate(old):
        role = "user" if i % 2 == 0 else "assistant"
        await agent.conversations.append(tid, mid, role, f"zebra migration {nonce} turn {i}")
    await agent.conversations.append(tid, uuid.uuid4().hex, "user", f"zebra migration {nonce} last")

    before = await agent.conversations.search(f"zebra migration {nonce}", thread_id=tid)
    assert set(old) <= {h["msg_id"] for h in before["hits"]}

    out = await agent.call(
        "conversation.compact", {"thread_id": tid, "keep_recent": 1, "min_to_compact": 2}
    )
    assert out["compacted"] is True, out

    after = await agent.conversations.search(f"zebra migration {nonce}", thread_id=tid)
    rows = (await agent.conversations.list(tid, limit=50))["rows"]
    listed = {r["msg_id"] for r in rows}
    hit_ids = {h["msg_id"] for h in after["hits"]}
    assert not hit_ids & set(old), after["hits"]
    assert hit_ids <= listed
    # The summary is embedded: the compacted content is still found through it.
    assert out["summary_msg_id"] in hit_ids, after["hits"]
    [summary] = [r for r in rows if r["msg_id"] == out["summary_msg_id"]]
    stored = ch_client.query(
        "SELECT agent, thread_id, role, content_excerpt, embedding_dim > 0, tombstoned_at, "
        "user_id, run_id, embed_attempts, embed_last_error, embed_failed_at, "
        "embedding_model != '', org, namespace, toDateTime(ts) "
        "FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND msg_id = {mid:String}",
        parameters={"org": agent.org, "ns": agent.namespace, "mid": out["summary_msg_id"]},
    ).result_rows
    [row] = [tuple(r) for r in stored]
    assert row[:14] == (
        agent.agent,
        tid,
        "system",
        summary["content"][:2048],
        True,
        None,
        "-",
        "-",
        1,
        "",
        None,
        True,
        agent.org,
        agent.namespace,
    )
    # Placed where the turns it replaces began, as the summary message is.
    assert row[14].replace(tzinfo=None) == datetime.fromisoformat(
        rows[0]["ts"].replace("Z", "+00:00")
    ).replace(tzinfo=None, microsecond=0)


@pytest.mark.asyncio
async def test_a_turn_another_agent_logged_survives_compaction(agent) -> None:
    """Two agents logging one turn share its embedding row: one agent's compaction
    leaves the turn searchable across agents while the other's copy is still live."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:10]
    other = f"{agent.agent}-peer"
    shared = uuid.uuid4().hex
    old = [shared, *(uuid.uuid4().hex for _ in range(3))]
    # The peer logs the turn first, so this agent's copy is the newer one, the row a
    # FINAL read keeps. Its own idempotency key: the SDK's is derived from the thread
    # and msg id alone.
    await agent.call(
        "conversation.append",
        {
            "thread_id": tid,
            "msg_id": shared,
            "role": "user",
            "content": f"okapi rollout {nonce} turn 0",
        },
        agent_id=other,
        idempotency_key=uuid.uuid4().hex,
    )
    for i, mid in enumerate(old):
        role = "user" if i % 2 == 0 else "assistant"
        await agent.conversations.append(tid, mid, role, f"okapi rollout {nonce} turn {i}")
    await agent.conversations.append(tid, uuid.uuid4().hex, "user", f"okapi rollout {nonce} last")

    out = await agent.call(
        "conversation.compact", {"thread_id": tid, "keep_recent": 1, "min_to_compact": 2}
    )
    assert out["compacted"] is True, out

    mine = await agent.conversations.search(f"okapi rollout {nonce}", thread_id=tid)
    assert not {h["msg_id"] for h in mine["hits"]} & set(old[1:]), mine["hits"]
    # Across agents ("*"): the shared row carries whichever agent wrote it last.
    every = await agent.conversations.search(f"okapi rollout {nonce}", thread_id=tid, agent_id="*")
    assert shared in {h["msg_id"] for h in every["hits"]}, every["hits"]


@pytest.mark.asyncio
async def test_a_deleted_thread_leaves_search_but_another_agents_turn_stays(
    agent, control_plane_url: str, ch_client, mint_test_token
) -> None:
    """conversation.delete tombstones the embeddings of every turn the agent logged in
    the thread, one compacted earlier included, and keeps the row of a turn another
    agent still holds."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:10]
    ids = [uuid.uuid4().hex for _ in range(4)]
    compacted, shared = ids[0], ids[1]
    # The peer logs the shared turn first, so this agent's copy is the row FINAL keeps.
    await agent.call(
        "conversation.append",
        {"thread_id": tid, "msg_id": shared, "role": "user", "content": f"ibex {nonce} 1"},
        agent_id=f"{agent.agent}-peer",
        idempotency_key=uuid.uuid4().hex,
    )
    for i, mid in enumerate(ids):
        role = "user" if i % 2 == 0 else "assistant"
        await agent.conversations.append(tid, mid, role, f"ibex {nonce} {i}")
    # A turn tombstoned while its embedding stays live.
    ch_client.command(
        "ALTER TABLE loom.conversations UPDATE tombstoned_at = now() "
        "WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String} "
        "AND msg_id = {mid:String} SETTINGS mutations_sync = 2",
        parameters={"org": agent.org, "ns": agent.namespace, "tid": tid, "mid": compacted},
    )

    out = await _admin_post(
        mint_test_token,
        agent,
        control_plane_url,
        "conversation.delete",
        {
            "org": agent.org,
            "namespace": agent.namespace,
            "agent": agent.agent,
            "thread_id": tid,
            "confirm_thread_id": tid,
        },
    )
    assert out["rows_affected"]["loom.conversations"] == 4
    assert out["rows_affected"]["loom.conversation_embeddings"] == 3  # all but the shared

    hits = await agent.conversations.search(f"ibex {nonce}", thread_id=tid, agent_id="*")
    assert {h["msg_id"] for h in hits["hits"]} == {shared}, hits["hits"]


@pytest.mark.asyncio
async def test_the_embedding_row_keeps_the_embedded_text_and_a_hit_shows_its_start(
    agent, ch_client
) -> None:
    """A long turn's embedding row keeps the 2,048 characters its vector was built
    from, which a re-embed reads; a search hit shows the first 240."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:10]
    content = f"gazelle {nonce} " * 200  # about 3,800 characters, all on topic
    mid = uuid.uuid4().hex
    await agent.conversations.append(tid, mid, "user", content)

    stored = ch_client.query(
        "SELECT agent, thread_id, role, content_excerpt, embedding_dim > 0, tombstoned_at, "
        "user_id, run_id, embed_attempts, embed_last_error, embedding_model != '' "
        "FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND msg_id = {mid:String}",
        parameters={"org": agent.org, "ns": agent.namespace, "mid": mid},
    ).result_rows
    assert [tuple(r) for r in stored] == [
        (agent.agent, tid, "user", content[:2048], True, None, "-", "-", 1, "", True)
    ]

    hits = await agent.conversations.search(f"gazelle {nonce}", thread_id=tid)
    [hit] = [h for h in hits["hits"] if h["msg_id"] == mid]
    assert hit["content_excerpt"] == content[:240]


@pytest.mark.asyncio
async def test_a_turn_another_agent_relogged_after_compaction_is_not_counted_on_delete(
    agent, control_plane_url: str, ch_client, mint_test_token
) -> None:
    """This agent's copy of a turn was compacted (message and embedding tombstoned);
    a peer then logs the turn again, a newer live row before any merge. Deleting the
    thread keeps that row and counts only the agent's turns whose embedding is
    tombstoned, not the peer's live row."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:10]
    shared, own = uuid.uuid4().hex, uuid.uuid4().hex
    await agent.conversations.append(tid, shared, "user", f"tapir {nonce} 0")
    await agent.conversations.append(tid, own, "assistant", f"tapir {nonce} 1")
    for table in ("loom.conversations", "loom.conversation_embeddings"):
        ch_client.command(
            f"ALTER TABLE {table} UPDATE tombstoned_at = now() "
            "WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String} "
            "AND msg_id = {mid:String} SETTINGS mutations_sync = 2",
            parameters={"org": agent.org, "ns": agent.namespace, "tid": tid, "mid": shared},
        )
    await agent.call(
        "conversation.append",
        {"thread_id": tid, "msg_id": shared, "role": "user", "content": f"tapir {nonce} 0"},
        agent_id=f"{agent.agent}-peer",
        idempotency_key=uuid.uuid4().hex,
    )

    out = await _admin_post(
        mint_test_token,
        agent,
        control_plane_url,
        "conversation.delete",
        {
            "org": agent.org,
            "namespace": agent.namespace,
            "agent": agent.agent,
            "thread_id": tid,
            "confirm_thread_id": tid,
        },
    )
    assert out["rows_affected"]["loom.conversation_embeddings"] == 1  # `own` only

    hits = await agent.conversations.search(f"tapir {nonce}", thread_id=tid, agent_id="*")
    assert {h["msg_id"] for h in hits["hits"]} == {shared}, hits["hits"]


@pytest.mark.asyncio
async def test_a_deleted_thread_takes_an_embedding_no_message_backs(
    agent, control_plane_url: str, ch_client, mint_test_token
) -> None:
    """A turn whose message row is gone (the conversation retention TTL expires message
    rows only) leaves its embedding row; deleting the thread tombstones it too."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:10]
    orphan = uuid.uuid4().hex
    await agent.conversations.append(tid, uuid.uuid4().hex, "user", f"quokka {nonce} 0")
    await agent.conversations.append(tid, orphan, "assistant", f"quokka {nonce} 1")
    ch_client.command(
        "ALTER TABLE loom.conversations DELETE "
        "WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String} "
        "AND msg_id = {mid:String} SETTINGS mutations_sync = 2",
        parameters={"org": agent.org, "ns": agent.namespace, "tid": tid, "mid": orphan},
    )

    out = await _admin_post(
        mint_test_token,
        agent,
        control_plane_url,
        "conversation.delete",
        {
            "org": agent.org,
            "namespace": agent.namespace,
            "agent": agent.agent,
            "thread_id": tid,
            "confirm_thread_id": tid,
        },
    )
    assert out["rows_affected"]["loom.conversation_embeddings"] == 2

    hits = await agent.conversations.search(f"quokka {nonce}", thread_id=tid, agent_id="*")
    assert hits["hits"] == [], hits["hits"]


@pytest.mark.asyncio
async def test_a_delete_counts_a_turn_once_however_many_models_embed_it(
    agent, control_plane_url: str, ch_client, mint_test_token
) -> None:
    """A re-embed to a new model leaves a turn with one row per model until the old
    rows are dropped: a delete tombstones every model's row and counts the turn once."""
    tid = f"thr-{uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:10]
    mids = [uuid.uuid4().hex for _ in range(2)]
    for i, mid in enumerate(mids):
        await agent.conversations.append(tid, mid, "user", f"okapi {nonce} {i}")
    ch_client.command(
        "INSERT INTO loom.conversation_embeddings "
        "SELECT * REPLACE ('next-model' AS embedding_model) "
        "FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String}",
        parameters={"org": agent.org, "ns": agent.namespace, "tid": tid},
    )
    copies = ch_client.query(
        "SELECT count() FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String} "
        "AND embedding_model = 'next-model'",
        parameters={"org": agent.org, "ns": agent.namespace, "tid": tid},
    ).result_rows[0][0]
    assert copies == 2  # the setup wrote the second model's rows

    out = await _admin_post(
        mint_test_token,
        agent,
        control_plane_url,
        "conversation.delete",
        {
            "org": agent.org,
            "namespace": agent.namespace,
            "agent": agent.agent,
            "thread_id": tid,
            "confirm_thread_id": tid,
        },
    )
    assert out["rows_affected"]["loom.conversation_embeddings"] == 2

    live = ch_client.query(
        "SELECT count() FROM loom.conversation_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND thread_id = {tid:String} "
        "AND tombstoned_at IS NULL",
        parameters={"org": agent.org, "ns": agent.namespace, "tid": tid},
    ).result_rows[0][0]
    assert live == 0
