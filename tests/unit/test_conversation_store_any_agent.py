"""The conversation store across agents: the substring fallback spans every agent on
"*"/"-", as the vector search does, and a thread read with no agent reads every
agent's turns."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.knowledge.domain.conversations import LISTED_MARGIN_CHARS, THREAD_EXCERPT_CHARS
from server.knowledge.store import conversations as conversation_repo

_AT = datetime(2026, 6, 1, 9, 0)


class StubCH:
    def __init__(self, rows: list[list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return self.rows.pop(0) if self.rows else []

    async def query_one(self, sql, params=None):
        found = await self.query_all(sql, params)
        return found[0] if found else None


@pytest.mark.asyncio
async def test_substring_search_spans_every_agent_on_a_wildcard() -> None:
    ch = StubCH()
    await conversation_repo.search_substring(
        "o", ["ns"], "bot", "deploy", scope=WHOLE_NAMESPACE, ch=ch
    )
    sql, params = ch.calls[-1]
    assert "agent = {agent:String}" in sql
    assert params["agent"] == "bot"

    for star in ("*", "-"):
        await conversation_repo.search_substring(
            "o", ["ns"], star, "deploy", scope=WHOLE_NAMESPACE, ch=ch
        )
        sql, params = ch.calls[-1]
        assert "agent = {agent:String}" not in sql
        assert "agent" not in params


@pytest.mark.asyncio
async def test_thread_reads_drop_the_agent_filter_for_any_agent() -> None:
    reads = (
        lambda agent, ch: conversation_repo.fetch_thread("o", "ns", agent, "t-1", ch=ch),
        lambda agent, ch: conversation_repo.count_thread_msgs("o", "ns", agent, "t-1", ch=ch),
        lambda agent, ch: conversation_repo.list_thread("o", "ns", agent, "t-1", ch=ch),
    )
    for read in reads:
        ch = StubCH()
        await read("bot", ch)
        sql, params = ch.calls[-1]
        assert "agent = {agent:String}" in sql
        assert params["agent"] == "bot"

        await read(None, ch)
        sql, params = ch.calls[-1]
        assert "agent = {agent:String}" not in sql
        assert "agent" not in params
        assert "thread_id = {tid:String}" in sql


@pytest.mark.asyncio
async def test_a_thread_counts_each_turn_once() -> None:
    # Two agents logging the same turn store one content-hashed msg_id twice.
    ch = StubCH()
    await conversation_repo.count_thread_msgs("o", "ns", None, "t-1", ch=ch)
    sql, _ = ch.calls[-1]
    # A turn is its id and its content: msg_id is unique per agent only, and a client may
    # number its own turns, so two agents' different turns can share one.
    assert "uniqExact(msg_id, cityHash64(content))" in sql


@pytest.mark.asyncio
async def test_a_thread_read_across_agents_returns_a_shared_turn_once() -> None:
    ch = StubCH()
    await conversation_repo.list_thread("o", "ns", None, "t-1", ch=ch)
    sql, _ = ch.calls[-1]
    assert "LIMIT 1 BY msg_id, cityHash64(content)" in sql

    # One agent never logs a msg_id twice (conversation.append checks), so its read needs no collapse.
    await conversation_repo.list_thread("o", "ns", "bot", "t-1", ch=ch)
    sql, _ = ch.calls[-1]
    assert "LIMIT 1 BY" not in sql


@pytest.mark.asyncio
async def test_a_paged_read_across_agents_collapses_before_the_cursor() -> None:
    """A second agent's copy of a turn is stamped later; collapsing inside the page would let
    it through the cursor onto the next page, so the collapse runs before the cursor."""
    from datetime import datetime

    ch = StubCH()
    await conversation_repo.list_thread(
        "o", "ns", None, "t-1", after=(datetime(2026, 6, 1, 9, 0), "m1"), ch=ch
    )
    sql, _ = ch.calls[-1]
    inner, _, outer = sql.partition("LIMIT 1 BY msg_id, cityHash64(content)")
    assert "(ts, msg_id) >" not in inner
    assert "(ts, msg_id) >" in outer


@pytest.mark.asyncio
async def test_the_thread_list_counts_turns_as_an_opened_thread_does() -> None:
    """The list ranks a namespace's threads with plain aggregates (an exact distinct count
    over the whole window holds every turn's key in memory), then counts the whole of each
    thread it returns, as conversation.get counts an opened one."""
    ch = StubCH(
        [
            [{"thread_id": "t-1", "msg_count": 5}, {"thread_id": "t-2", "msg_count": 2}],
            [
                {"thread_id": "t-1", "n": 4, "last_turn": ("bot", "user", "hi")},
                {"thread_id": "t-2", "n": 2, "last_turn": ("bot", "user", "hi")},
            ],
        ]
    )
    rows = await conversation_repo.recent_threads("o", "ns", window_days=30, limit=10, ch=ch)
    ranked, counted = ch.calls[0][0], ch.calls[1][0]
    assert "uniqExact" not in ranked
    assert "uniqExact(msg_id, cityHash64(content))" in counted
    assert "thread_id IN {tids:Array(String)}" in counted
    assert "toIntervalDay" not in counted
    assert ch.calls[1][1]["tids"] == ["t-1", "t-2"]
    assert [(r["thread_id"], r["msg_count"]) for r in rows] == [("t-1", 4), ("t-2", 2)]
    assert rows[0]["last_agent"] == "bot" and rows[0]["last_content"] == "hi"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fields", "keyed_read", "reads_last_turn", "served"),
    [
        # Only the title: no keyed read, and the last turn's fields are empty.
        (["title"], False, False, (1, "", "", "Titled")),
        # The role: the keyed read takes the last turn.
        (["last_role"], True, True, (1, "user", "hi", "Titled")),
        # The count alone: the keyed read counts, and reads no last turn.
        (["msg_count"], True, False, (7, "", "", "Titled")),
    ],
)
async def test_a_thread_list_reads_the_last_turn_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
    fields: list[str],
    keyed_read: bool,
    reads_last_turn: bool,
    served: tuple[int, str, str, str],
) -> None:
    """`fields` without the agent, role, excerpt or count is one statement over the
    transcript; asking for the count or the last turn adds the keyed read, which reads
    the last turn only when one of its fields is asked for."""
    from server.knowledge.api.models_conversations import ConversationThreadsInput
    from server.knowledge.app.conversations import handle_conversation_threads

    grouped = [{"thread_id": "t-1", "msg_count": 1, "first_seen": _AT, "last_seen": _AT}]
    keyed_row = {"thread_id": "t-1", "n": 7}
    if reads_last_turn:
        keyed_row["last_turn"] = ("bot", "user", "hi")
    titles = [{"thread_id": "t-1", "title": "Titled"}]
    ch = StubCH([grouped, [keyed_row], titles] if keyed_read else [grouped, titles])
    monkeypatch.setattr(conversation_repo, "_default_client", lambda: ch)
    out = await handle_conversation_threads(
        ConversationThreadsInput(org="o", namespace="ns", agent="a", fields=fields)
    )
    transcript = [sql for sql, _ in ch.calls if "FROM loom.conversations\n" in sql]
    assert len(transcript) == (2 if keyed_read else 1)
    if keyed_read:
        assert ("argMax" in transcript[1]) is reads_last_turn
    [thread] = out.threads
    assert (
        thread.msg_count,
        thread.last_role,
        thread.last_content_excerpt,
        thread.title,
    ) == served


@pytest.mark.asyncio
async def test_the_saved_list_reads_the_prefix_it_checks() -> None:
    """The saved list never carries a long message whole: the aggregate reads the first
    `listed` characters, which the listing checks and cuts."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    ch = StubCH()
    await conversation_repo.list_saved_messages(
        "o", "ns", "a", scope=WHOLE_NAMESPACE, user_id="u-1", listed=1264, ch=ch
    )
    sql, params = ch.calls[0]
    assert "argMax(substringUTF8(content, 1, {listed:UInt32}), ts) AS content" in sql
    assert "argMax(content," not in sql and params["listed"] == 1264


@pytest.mark.asyncio
async def test_a_thread_gone_from_the_keyed_read_is_left_out() -> None:
    # Its last live turn was deleted between the two statements.
    ch = StubCH(
        [
            [{"thread_id": "t-1", "msg_count": 5}, {"thread_id": "t-2", "msg_count": 2}],
            [{"thread_id": "t-1", "n": 4, "last_turn": ("bot", "user", "hi")}],
        ]
    )
    rows = await conversation_repo.recent_threads("o", "ns", window_days=30, limit=10, ch=ch)
    assert [r["thread_id"] for r in rows] == ["t-1"]


@pytest.mark.asyncio
async def test_a_thread_list_that_asks_for_no_turn_or_count_is_one_statement() -> None:
    ch = StubCH([[{"thread_id": "t-1", "msg_count": 5}]])
    rows = await conversation_repo.recent_threads(
        "o", "ns", window_days=30, limit=10, count_turns=False, last_turn=False, ch=ch
    )
    assert len(ch.calls) == 1
    assert rows == [
        {
            "thread_id": "t-1",
            "msg_count": 5,
            "last_agent": "",
            "last_role": "",
            "last_content": "",
        }
    ]


@pytest.mark.asyncio
async def test_an_empty_thread_list_asks_no_count() -> None:
    ch = StubCH([[]])
    assert await conversation_repo.recent_threads("o", "ns", window_days=30, limit=10, ch=ch) == []
    assert len(ch.calls) == 1


@pytest.mark.asyncio
async def test_a_read_across_agents_orders_two_turns_that_tie_on_time_and_id() -> None:
    # Two agents' different turns can share a client-chosen msg_id and a millisecond: the
    # content hash breaks the tie, so offsets and page order are stable.
    ch = StubCH()
    await conversation_repo.list_thread("o", "ns", None, "t-1", ch=ch)
    sql, _ = ch.calls[-1]
    assert sql.count("ORDER BY ts ASC, msg_id ASC, cityHash64(content) ASC") == 2


@pytest.mark.asyncio
async def test_a_thread_list_that_asks_no_count_reads_no_count() -> None:
    # The keyed read still runs, for the last turn the listing checks for a high shape,
    # but selects no count: the page keeps the window's.
    ch = StubCH(
        [
            [{"thread_id": "t-1", "msg_count": 5}],
            [{"thread_id": "t-1", "n": 9, "last_turn": ("bot", "user", "hi")}],
        ]
    )
    rows = await conversation_repo.recent_threads(
        "o", "ns", window_days=30, limit=10, count_turns=False, ch=ch
    )
    keyed, params = ch.calls[1]
    # No count is selected, and the last turn is read only as far as the listing checks.
    assert " AS n" not in keyed and "uniqExact" not in keyed
    assert "substringUTF8(content, 1, {listed:UInt32})" in keyed
    assert params["listed"] == THREAD_EXCERPT_CHARS + LISTED_MARGIN_CHARS
    assert rows[0]["msg_count"] == 5


@pytest.mark.asyncio
async def test_a_paged_read_for_one_agent_is_one_statement() -> None:
    """A cursor page for a named agent filters in one WHERE, as before the cross-agent read
    wrapped its source; a second WHERE would be a syntax error."""
    from datetime import datetime

    ch = StubCH()
    await conversation_repo.list_thread(
        "o", "ns", "bot", "t-1", after=(datetime(2026, 6, 1, 9, 0), "m1"), ch=ch
    )
    sql, params = ch.calls[-1]
    assert sql.count("WHERE") == 1
    assert "(ts, msg_id) >" in sql
    assert params["agent"] == "bot"


@pytest.mark.asyncio
async def test_one_agents_count_is_the_rows_its_list_pages_through() -> None:
    # A named agent's list does not collapse copies, so its count does not either.
    ch = StubCH()
    await conversation_repo.count_thread_msgs("o", "ns", "bot", "t-1", ch=ch)
    sql, _ = ch.calls[-1]
    assert "count() AS n" in sql


@pytest.mark.asyncio
async def test_a_substring_search_across_agents_collapses_before_its_limit() -> None:
    # Collapsing after the limit would fill the page with copies and return fewer turns
    # than match; the turn key is the full text, as the thread read's.
    ch = StubCH()
    await conversation_repo.search_substring(
        "o", ["ns"], "*", "deploy", limit=5, scope=WHOLE_NAMESPACE, ch=ch
    )
    sql, _ = ch.calls[-1]
    assert sql.index("LIMIT 1 BY namespace, thread_id, msg_id, cityHash64(content)") < sql.index(
        "LIMIT {limit:UInt32}"
    )

    await conversation_repo.search_substring(
        "o", ["ns"], "bot", "deploy", limit=5, scope=WHOLE_NAMESPACE, ch=ch
    )
    sql, _ = ch.calls[-1]
    assert "LIMIT 1 BY" not in sql


@pytest.mark.asyncio
async def test_a_substring_hit_names_a_shared_turns_first_copy() -> None:
    # The thread read keeps a turn's earliest copy; a hit opens that turn, so it names the
    # same copy: the collapse runs oldest first, the page newest first.
    ch = StubCH()
    await conversation_repo.search_substring(
        "o", ["ns"], "*", "deploy", limit=5, scope=WHOLE_NAMESPACE, ch=ch
    )
    sql, _ = ch.calls[-1]
    inner, _, outer = sql.partition("LIMIT 1 BY")
    assert "ORDER BY ts ASC" in inner
    assert "ORDER BY ts DESC" in outer


@pytest.mark.asyncio
async def test_a_shared_turns_first_copy_is_the_same_row_on_every_read() -> None:
    # Two agents can log a turn in one millisecond: the agent breaks that tie, so the copy a
    # read keeps does not depend on which part ClickHouse delivers first.
    ch = StubCH()
    await conversation_repo.list_thread("o", "ns", None, "t-1", ch=ch)
    inner = ch.calls[-1][0].partition("LIMIT 1 BY")[0]
    assert "ORDER BY ts ASC, msg_id ASC, cityHash64(content) ASC, agent ASC" in inner
    await conversation_repo.search_substring(
        "o", ["ns"], "*", "deploy", limit=5, scope=WHOLE_NAMESPACE, ch=ch
    )
    inner = ch.calls[-1][0].partition("LIMIT 1 BY")[0]
    assert "ORDER BY ts ASC, agent ASC" in inner


@pytest.mark.asyncio
async def test_the_thread_list_counts_one_agents_rows_as_its_opened_thread_does() -> None:
    # conversation.get counts a named agent's rows as stored (count_thread_msgs); the list
    # counts them the same way, so a raced double append reads N+1 in both.
    ch = StubCH(
        [
            [{"thread_id": "t-1", "msg_count": 5}],
            [{"thread_id": "t-1", "n": 5, "last_turn": ("bot", "user", "hi")}],
        ]
    )
    await conversation_repo.recent_threads("o", "ns", window_days=30, limit=10, agent="bot", ch=ch)
    counted = ch.calls[1][0]
    assert "count() AS n" in counted
    assert "uniqExact" not in counted


@pytest.mark.asyncio
async def test_the_thread_list_pages_past_its_first_page() -> None:
    """`offset` skips the threads earlier pages showed, in the same most-recent-first
    order, so a namespace's every thread in the window can be listed page by page."""
    ch = StubCH(
        [
            [{"thread_id": "t-3", "msg_count": 1}],
            [{"thread_id": "t-3", "n": 1, "last_turn": ("bot", "user", "hi")}],
        ]
    )
    rows = await conversation_repo.recent_threads(
        "o", "ns", window_days=30, limit=2, offset=2, ch=ch
    )
    ranked, params = ch.calls[0]
    assert "ORDER BY last_seen DESC, thread_id ASC" in ranked
    assert "LIMIT {lim:UInt32} OFFSET {off:UInt32}" in ranked
    assert (params["lim"], params["off"]) == (2, 2)
    assert [r["thread_id"] for r in rows] == ["t-3"]
