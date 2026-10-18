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
async def test_threads_offset_pages_neither_repeat_nor_drop_a_thread(agent) -> None:
    """Five threads walked two at a time: every thread appears on exactly one page."""
    tids = [f"thr-{uuid.uuid4().hex[:6]}" for _ in range(5)]
    for tid in tids:
        await agent.conversations.append(tid, uuid.uuid4().hex, "user", f"hello from {tid}")

    seen: list[str] = []
    for offset in range(0, 8, 2):
        page = await agent.call("conversation.threads", limit=2, offset=offset, agent="*")
        assert len(page["threads"]) <= 2
        seen.extend(t["thread_id"] for t in page["threads"])

    assert sorted(seen) == sorted(tids)
