"""Unit tests for conversation.get — single-thread fetch with optional messages.

Boundaries we stub:
  - conversation_repo.fetch_thread       (thread metadata point lookup)
  - conversation_repo.count_thread_msgs  (msg_count for the thread)
  - conversation_repo.list_thread        (messages slice when included)
"""

from __future__ import annotations

from datetime import datetime

import pytest


def _thread(
    *,
    thread_id: str = "t-1",
    title: str = "Q1 incident response",
    metadata: str = '{"owner":"alice"}',
    agent: str = "reviewer",
) -> dict:
    return {
        "thread_id": thread_id,
        "title": title,
        "metadata": metadata,
        "run_id": "run-7",
        "user_id": "alice",
        "agent": agent,
        "created_at": datetime(2026, 5, 18, 12, 0, 0),
        "updated_at": datetime(2026, 5, 18, 12, 5, 0),
    }


def _msg(*, msg_id: str, role: str, content: str, ts: datetime, agent: str = "reviewer"):
    from server.knowledge.store.conversations import MessageRow

    return MessageRow(
        org="clickhouse",
        namespace="finance",
        agent=agent,
        thread_id="t-1",
        msg_id=msg_id,
        role=role,
        content=content,
        reasoning="",
        user_id="alice",
        run_id="run-7",
        metadata="{}",
        tokens=0,
        ts=ts,
    )


@pytest.mark.asyncio
async def test_thread_missing_returns_found_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch_thread(*a, **kw):
        return None

    async def fake_count(*a, **kw):
        return 0

    list_called = {"v": False}

    async def fake_list_thread(*a, **kw):
        list_called["v"] = True
        return []

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="ghost",
        )
    )
    # No metadata row AND no messages → genuinely absent.
    assert out.found is False
    assert out.thread_id == "ghost"
    assert out.msg_count == 0
    assert out.messages == []
    # Missing thread → skip message fetch entirely.
    assert list_called["v"] is False


@pytest.mark.asyncio
async def test_append_only_thread_is_openable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread built purely by conversation.append (no conversation.create,
    so no metadata row) must still open: the thread LIST is derived from
    messages, so message presence alone means the thread exists."""
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch_thread(*a, **kw):
        return None  # never created → no metadata row

    async def fake_count(*a, **kw):
        return 4

    async def fake_list_thread(*a, **kw):
        return [
            _msg(msg_id="m1", role="user", content="hi", ts=datetime(2026, 6, 1, 9, 0)),
            _msg(msg_id="m2", role="assistant", content="hello", ts=datetime(2026, 6, 1, 9, 1)),
        ]

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="claude",
            thread_id="alice-first-chat",
            include_messages=True,
        )
    )
    assert out.found is True
    assert out.msg_count == 4
    assert out.title == ""  # no metadata row → empty, not an error
    assert out.agent == "claude"  # falls back to the requested agent
    assert [m.msg_id for m in out.messages] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_thread_found_without_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch_thread(*a, **kw):
        return _thread()

    async def fake_count(*a, **kw):
        return 42

    list_called = {"v": False}

    async def fake_list_thread(*a, **kw):
        list_called["v"] = True
        return []

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
            # include_messages defaults to False.
        )
    )
    assert out.found is True
    assert out.title == "Q1 incident response"
    assert out.metadata == {"owner": "alice"}
    assert out.agent == "reviewer"
    assert out.run_id == "run-7"
    assert out.user_id == "alice"
    assert out.msg_count == 42
    assert out.messages == []
    # No message slice → no list_thread call.
    assert list_called["v"] is False


@pytest.mark.asyncio
async def test_thread_found_with_messages_paginated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_messages=True with limit/offset slices the result."""
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch_thread(*a, **kw):
        return _thread()

    async def fake_count(*a, **kw):
        return 5

    captured = {}

    async def fake_list_thread(
        org,
        ns,
        agent,
        thread_id,
        *,
        run_id=None,
        after=None,
        limit=500,
        order="asc",
        ch=None,
    ):
        captured["limit"] = limit
        captured["order"] = order
        # Return 5 rows; handler should slice [offset:] then take limit.
        return [
            _msg(
                msg_id=f"m-{i}",
                role="user",
                content=f"msg-{i}",
                ts=datetime(2026, 5, 18, 12, i, 0),
            )
            for i in range(5)
        ]

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
            include_messages=True,
            limit=2,
            offset=1,
        )
    )
    assert out.found is True
    assert out.msg_count == 5
    # Handler asks for offset + limit = 3 rows and one more (does a page follow?),
    # then drops the first.
    assert captured["limit"] == 4
    assert captured["order"] == "asc"
    assert [m.msg_id for m in out.messages] == ["m-1", "m-2"]


@pytest.mark.asyncio
async def test_order_desc_pages_from_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """order="desc": the repository is asked for the newest rows and the page
    is counted from the tail, so offset 0 / limit N is the last N turns,
    newest first — one call instead of fetch-everything-and-slice."""
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch_thread(*a, **kw):
        return _thread()

    async def fake_count(*a, **kw):
        return 5

    captured = {}

    async def fake_list_thread(org, ns, agent, thread_id, *, limit=500, order="asc", **kw):
        captured["limit"] = limit
        captured["order"] = order
        # The repository honours the order: newest first when asked.
        return [
            _msg(
                msg_id=f"m-{i}", role="user", content=f"msg-{i}", ts=datetime(2026, 5, 18, 12, i, 0)
            )
            for i in (4, 3, 2, 1, 0)
        ][:limit]

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
            include_messages=True,
            limit=2,
            order="desc",
        )
    )
    assert captured == {"limit": 2, "order": "desc"}
    assert [m.msg_id for m in out.messages] == ["m-4", "m-3"]

    # offset counts from the tail too: skip the newest, take the next two.
    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
            include_messages=True,
            limit=2,
            offset=1,
            order="desc",
        )
    )
    assert captured == {"limit": 3, "order": "desc"}
    assert [m.msg_id for m in out.messages] == ["m-3", "m-2"]


def test_order_rejects_anything_but_asc_or_desc() -> None:
    from pydantic import ValidationError

    from server.knowledge.api.models_conversations import ConversationGetInput

    with pytest.raises(ValidationError):
        ConversationGetInput(org="o", namespace="n", agent="a", thread_id="t", order="newest")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reads_run_with_sequential_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A get that follows the caller's own append must see the turn, and
    the count and the slice must agree: every read runs inside the
    sequential-reads scope (otherwise a client on a multi-replica service can
    see msg_count=1 with zero messages for seconds)."""
    from server.core.ch import sequential_reads_active
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    seen: list[tuple[str, bool]] = []

    async def fake_fetch_thread(*a, **kw):
        seen.append(("fetch_thread", sequential_reads_active()))
        return _thread()

    async def fake_count(*a, **kw):
        seen.append(("count", sequential_reads_active()))
        return 1

    async def fake_list_thread(*a, **kw):
        seen.append(("list", sequential_reads_active()))
        return [
            _msg(msg_id="m-0", role="assistant", content="x", ts=datetime(2026, 5, 18, 12, 0, 0))
        ]

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    assert sequential_reads_active() is False
    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
            include_messages=True,
        )
    )
    assert out.msg_count == 1 and len(out.messages) == 1
    assert seen == [("fetch_thread", True), ("count", True), ("list", True)]
    # …and nothing leaks out of the handler.
    assert sequential_reads_active() is False


@pytest.mark.asyncio
async def test_thread_with_malformed_metadata_does_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread row with invalid JSON metadata should still surface
    (metadata = {}). Forward-compat against legacy / hand-edited rows."""
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch_thread(*a, **kw):
        return _thread(metadata="this is not json")

    async def fake_count(*a, **kw):
        return 0

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
        )
    )
    assert out.found is True
    assert out.metadata == {}  # safely defaults rather than raising


@pytest.mark.asyncio
async def test_operation_registered_with_viewer_role() -> None:
    from server.knowledge.api.ops_conversations import OPERATIONS
    from server.operations.base import Role

    op = next(o for o in OPERATIONS if o.name == "conversation.get")
    assert op.required_role == Role.VIEWER
    assert op.idempotent is True
    assert op.audit is False


@pytest.mark.asyncio
async def test_any_agent_reads_the_whole_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent="*" (a caller that knows the thread id but not who wrote it: a search
    hit, a memory's source link) reads every agent's turns, and the thread row's
    owner and title."""
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    read_as: list[str | None] = []

    async def fake_fetch_thread(org, namespace, agent, thread_id, **kw):
        read_as.append(agent)
        return _thread(agent="planner")

    async def fake_count(org, namespace, agent, thread_id, **kw):
        read_as.append(agent)
        return 2

    async def fake_list_thread(org, namespace, agent, thread_id, **kw):
        read_as.append(agent)
        return [_msg(msg_id="m1", role="user", content="hi", ts=datetime(2026, 6, 1, 9, 0))]

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse", namespace="finance", agent="*", thread_id="t-1", include_messages=True
        )
    )
    assert out.found is True
    assert out.agent == "planner"
    assert out.title == "Q1 incident response"
    assert read_as == [None, None, None]


@pytest.mark.asyncio
async def test_any_agent_names_an_append_only_threads_latest_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch_thread(*a, **kw):
        return None

    async def fake_count(*a, **kw):
        return 2

    async def fake_list_thread(*a, **kw):
        # Newest first, as order="desc" returns them: the latest writer is not the first row read.
        return [
            _msg(
                msg_id="m1",
                role="user",
                content="hi",
                ts=datetime(2026, 6, 1, 9, 0),
                agent="planner",
            ),
            _msg(
                msg_id="m3",
                role="assistant",
                content="done",
                ts=datetime(2026, 6, 1, 9, 2),
                agent="deployer",
            ),
            _msg(msg_id="m2", role="assistant", content="hello", ts=datetime(2026, 6, 1, 9, 1)),
        ]

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse", namespace="finance", agent="*", thread_id="t-1", include_messages=True
        )
    )
    assert out.found is True
    assert out.agent == "deployer"
    # Each turn names its own writer.
    assert [(m.msg_id, m.agent) for m in out.messages] == [
        ("m1", "planner"),
        ("m3", "deployer"),
        ("m2", "reviewer"),
    ]


@pytest.mark.asyncio
async def test_dash_reads_every_agent_as_star_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    read_as: list[str | None] = []

    async def fake_fetch_thread(org, namespace, agent, thread_id, **kw):
        read_as.append(agent)
        return None

    async def fake_count(org, namespace, agent, thread_id, **kw):
        read_as.append(agent)
        return 0

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)

    await handle_conversation_get(
        ConversationGetInput(org="clickhouse", namespace="finance", agent="-", thread_id="t-1")
    )
    # One meaning for the sentinels on every conversation op, search included.
    assert read_as == [None, None]


@pytest.mark.asyncio
async def test_list_with_any_agent_pages_the_whole_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationListInput
    from server.knowledge.app.conversations import handle_conversation_list
    from server.knowledge.store import conversations as conversation_repo

    async def fake_list_thread(org, namespace, agent, thread_id, **kw):
        assert agent is None
        return [
            _msg(
                msg_id="m1",
                role="user",
                content="hi",
                ts=datetime(2026, 6, 1, 9, 0),
                agent="planner",
            )
        ]

    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_list(
        ConversationListInput(org="clickhouse", namespace="finance", agent="*", thread_id="t-1")
    )
    assert [(r.msg_id, r.agent) for r in out.rows] == [("m1", "planner")]


@pytest.mark.asyncio
async def test_a_longer_thread_answers_the_cursor_conversation_list_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first page carries the next page's cursor, issued the way
    conversation.list issues it, so the caller pages on with that op."""
    from server.core.cursors import decode_cursor
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    turns = [
        _msg(msg_id=f"m{i}", role="user", content="hi", ts=datetime(2026, 6, 1, 9, i))
        for i in range(3)
    ]

    async def fake_fetch_thread(*a, **kw):
        return _thread()

    async def fake_count(*a, **kw):
        return 3

    async def fake_list_thread(*a, limit, **kw):
        return turns[:limit]

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    def get(**kw):
        return handle_conversation_get(
            ConversationGetInput(
                org="clickhouse",
                namespace="finance",
                agent="*",
                thread_id="t-1",
                include_messages=True,
                **kw,
            )
        )

    out = await get(limit=2)
    assert [m.msg_id for m in out.messages] == ["m0", "m1"]
    assert out.next_cursor is not None
    assert decode_cursor(out.next_cursor) == (turns[1].ts, "m1")

    assert (await get(limit=3)).next_cursor is None
    # A tail page counts back from the newest turn; conversation.list's cursor walks forward.
    assert (await get(limit=2, order="desc")).next_cursor is None


def test_an_entry_from_a_previous_release_has_no_agent() -> None:
    from server.knowledge.api.models_conversations import _ConversationEntry

    entry = _ConversationEntry.model_validate(
        {
            "msg_id": "m1",
            "role": "user",
            "content": "hi",
            "reasoning": "",
            "user_id": "-",
            "run_id": "-",
            "metadata": {},
            "tokens": 0,
            "ts": "2026-06-01T09:00:00",
        }
    )
    assert entry.agent == ""


@pytest.mark.asyncio
async def test_the_latest_writer_is_read_from_the_page_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationGetInput
    from server.knowledge.app.conversations import handle_conversation_get
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch_thread(*a, **kw):
        return None

    async def fake_count(*a, **kw):
        return 2

    async def fake_list_thread(*a, **kw):
        # The second row is the peek past a one-turn page, by another agent.
        return [
            _msg(
                msg_id="m1",
                role="user",
                content="hi",
                ts=datetime(2026, 6, 1, 9, 0),
                agent="planner",
            ),
            _msg(
                msg_id="m2",
                role="user",
                content="yo",
                ts=datetime(2026, 6, 1, 9, 1),
                agent="deployer",
            ),
        ]

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch_thread)
    monkeypatch.setattr(conversation_repo, "count_thread_msgs", fake_count)
    monkeypatch.setattr(conversation_repo, "list_thread", fake_list_thread)

    out = await handle_conversation_get(
        ConversationGetInput(
            org="clickhouse",
            namespace="finance",
            agent="*",
            thread_id="t-1",
            include_messages=True,
            limit=1,
        )
    )
    assert out.agent == "planner"


@pytest.mark.asyncio
async def test_threads_reads_dash_as_every_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationThreadsInput
    from server.knowledge.app.conversations import handle_conversation_threads
    from server.knowledge.store import conversations as conversation_repo

    seen: list[str | None] = []

    async def fake_recent(org, namespace, *, agent=None, **kw):
        seen.append(agent)
        return []

    async def fake_titles(*a, **kw):
        return {}

    monkeypatch.setattr(conversation_repo, "recent_threads", fake_recent)
    monkeypatch.setattr(conversation_repo, "thread_titles", fake_titles)
    for sentinel in ("*", "-"):
        await handle_conversation_threads(
            ConversationThreadsInput(org="clickhouse", namespace="finance", agent=sentinel)
        )
    assert seen == [None, None]


@pytest.mark.asyncio
async def test_threads_passes_its_offset_to_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationThreadsInput
    from server.knowledge.app.conversations import handle_conversation_threads
    from server.knowledge.store import conversations as conversation_repo

    seen: list[int] = []

    async def fake_recent(org, namespace, *, offset=0, **kw):
        seen.append(offset)
        return []

    async def fake_titles(*a, **kw):
        return {}

    monkeypatch.setattr(conversation_repo, "recent_threads", fake_recent)
    monkeypatch.setattr(conversation_repo, "thread_titles", fake_titles)
    await handle_conversation_threads(ConversationThreadsInput(org="o", namespace="ns"))
    await handle_conversation_threads(ConversationThreadsInput(org="o", namespace="ns", offset=50))
    assert seen == [0, 50]


def test_threads_rejects_an_offset_past_its_bound() -> None:
    """An offset past 100,000 is a 422, not a silent first page (the store binds it as UInt32)."""
    from pydantic import ValidationError

    from server.knowledge.api.models_conversations import ConversationThreadsInput

    assert ConversationThreadsInput(org="o", namespace="ns", offset=100_000).offset == 100_000
    with pytest.raises(ValidationError):
        ConversationThreadsInput(org="o", namespace="ns", offset=100_001)
