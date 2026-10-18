"""Conversations plane — append, list, search."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
async def _recording_on(agent, control_plane_url: str):
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
    import httpx

    from server.auth.jwt import mint_token

    tok = mint_token(
        subject="test-suite-admin",
        org=agent.org,
        namespaces=["*"],
        roles=["admin"],
        ttl_seconds=600,
    )
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30.0) as c:
        r = await c.post(
            "/v1/conversation.set_logging",
            json={"org": agent.org, "namespace": agent.namespace, "enabled": True},
            headers={"Authorization": f"Bearer {tok}"},
        )
        r.raise_for_status()


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
async def test_a_compacted_turn_leaves_search(agent) -> None:
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
    listed = {r["msg_id"] for r in (await agent.conversations.list(tid, limit=50))["rows"]}
    hit_ids = {h["msg_id"] for h in after["hits"]}
    assert not hit_ids & set(old), after["hits"]
    assert hit_ids <= listed


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
    agent, control_plane_url: str, ch_client
) -> None:
    """conversation.delete tombstones the embeddings of every turn the agent logged in
    the thread, one compacted earlier included, and keeps the row of a turn another
    agent still holds."""
    import httpx

    from server.auth.jwt import mint_token

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

    tok = mint_token(
        subject="test-suite-admin",
        org=agent.org,
        namespaces=["*"],
        roles=["admin"],
        ttl_seconds=600,
    )
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30.0) as c:
        r = await c.post(
            "/v1/conversation.delete",
            json={
                "org": agent.org,
                "namespace": agent.namespace,
                "agent": agent.agent,
                "thread_id": tid,
                "confirm_thread_id": tid,
            },
            headers={"Authorization": f"Bearer {tok}"},
        )
        r.raise_for_status()
        out = r.json()
    assert out["rows_affected"]["loom.conversations"] == 4
    assert out["rows_affected"]["loom.conversation_embeddings"] == 3  # all but the shared

    hits = await agent.conversations.search(f"ibex {nonce}", thread_id=tid, agent_id="*")
    assert {h["msg_id"] for h in hits["hits"]} == {shared}, hits["hits"]
