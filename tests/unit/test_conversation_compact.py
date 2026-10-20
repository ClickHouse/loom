"""conversation.compact reads a plain MergeTree (FINAL is illegal there, code
181) and writes a summary row that erasure-by-user must be able to find."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.knowledge.api.models_conversations import ConversationCompactInput
from server.knowledge.app import conversations as ch_mod
from server.llm.client import LLMResult
from tests.unit._fake_credentials import OPENAI_KEY


@pytest.fixture(autouse=True)
def _saved_write_leases(monkeypatch):
    from tests.unit.claims_fake import held_leases

    held_leases(monkeypatch)


T0 = datetime(2026, 9, 1, 12, 0, 0)


def _turn(
    i: int, *, secret: bool = False, saved: bool = False, run_id: str = "-"
) -> dict[str, Any]:
    """Transcript row `m{i}`, `i` seconds after T0; `secret` puts `OPENAI_KEY` in its text."""
    return {
        "msg_id": f"m{i}",
        "role": "user",
        "content": f"my key is {OPENAI_KEY}" if secret else f"turn {i}",
        "ts": T0 + timedelta(seconds=i),
        "metadata": "{}",
        "user_id": "alice",
        "run_id": run_id,
        "is_saved": saved,
    }


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.inserted: list[dict[str, Any]] = []  # loom.conversations rows
        self.embedded: list[dict[str, Any]] = []  # loom.conversation_embeddings rows
        self.sql: list[str] = []
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sql.append(sql)
        # ClickHouse answers FINAL on a plain MergeTree with code 181.
        assert "loom.conversations FINAL" not in sql, "FINAL on plain MergeTree"
        # The embeddings reconcile's re-read finds no row left to reconcile.
        return [] if "FROM loom.conversation_embeddings FINAL" in sql else self.rows

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        return None  # the shared embedding cache reads it as a miss

    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        (self.embedded if table == "loom.conversation_embeddings" else self.inserted).extend(rows)

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **kw: Any) -> None:
        self.sql.append(sql)
        self.executed.append((sql, params or {}))


_EMBEDDING_FIELDS = (
    "org",
    "namespace",
    "agent",
    "thread_id",
    "msg_id",
    "role",
    "content_excerpt",
    "user_id",
    "run_id",
    "ts",
    "embed_attempts",
    "embed_last_error",
    "embed_failed_at",
)


class _FakeLLM:
    provider = "fake"
    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []  # each call's user prompt

    async def complete(self, **kw: Any) -> LLMResult:
        self.prompts.append(kw["user"])
        return LLMResult(text="a summary", tokens_in=1, tokens_out=2, provider="fake", model="fake")


def test_compact_reads_without_final_and_carries_the_threads_user(monkeypatch) -> None:
    import server.core.ch
    import server.llm.client

    rows = [
        {
            "msg_id": f"m{i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}",
            "ts": T0 + timedelta(seconds=i),
            "metadata": "{}",
            "user_id": "alice",
            "run_id": "run-1",
        }
        for i in range(8)
    ]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _FakeLLM())

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=2, min_to_compact=2
            )
        )
    )
    assert out.compacted is True
    [summary] = fake.inserted
    assert summary["user_id"] == "alice"
    assert summary["run_id"] == "run-1"
    assert summary["ts"] == T0
    # The summary is embedded, so search finds what it replaced.
    [vec] = fake.embedded
    assert {k: vec[k] for k in _EMBEDDING_FIELDS} == {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "thread_id": "t",
        "msg_id": summary["msg_id"],
        "role": "system",
        "content_excerpt": "a summary",
        "user_id": "alice",  # erasure-by-user finds the summary's vector too
        "run_id": "run-1",
        "ts": T0,
        "embed_attempts": 1,
        "embed_last_error": "",
        "embed_failed_at": None,
    }
    assert vec["embedding_dim"] > 0 and len(vec["embedding"]) == vec["embedding_dim"]
    assert vec["embedding_model"] == ch_mod.get_embedder().model_name


def test_compact_tombstones_only_the_messages_the_summary_read(monkeypatch) -> None:
    """The window is cut where the prompt budget ends: a message the summarizer never
    saw stays verbatim for the next run instead of being tombstoned."""
    import server.core.ch
    import server.llm.client

    long = "x" * 7000
    rows = [
        {
            "msg_id": f"m{i}",
            "role": "user",
            "content": long,
            "ts": T0 + timedelta(seconds=i),
            "metadata": "{}",
            "user_id": "alice",
            "run_id": "run-1",
        }
        for i in range(6)
    ]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _FakeLLM())

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=2
            )
        )
    )
    # Five candidates of ~7 KB each; two fit the 16,000-character prompt.
    assert out.compacted is True and out.messages_summarized == 2
    [summary] = fake.inserted
    assert '"compacted_from": ["m0", "m1"]' in summary["metadata"]

    # The compacted turns leave conversation.search too: their embeddings are
    # tombstoned by the same ids as the messages (the live ClickHouse tests cover what
    # the passes produce, a retry after a failure included).
    tombstoned = {
        table: params["ids"]
        for sql, params in fake.executed
        for table, marker in (
            ("loom.conversations", "ALTER TABLE loom.conversations\n"),
            ("loom.conversation_embeddings", "AS tombstoned_at"),
        )
        if marker in sql
    }
    assert tombstoned == {
        "loom.conversations": ["m0", "m1"],
        "loom.conversation_embeddings": ["m0", "m1"],
    }


def test_fitting_prefix_keeps_at_least_one_line() -> None:
    assert ch_mod._fitting_prefix(["a" * 10, "b" * 10], 21) == 2
    assert ch_mod._fitting_prefix(["a" * 10, "b" * 10], 20) == 1
    assert ch_mod._fitting_prefix(["a" * 50], 20) == 1


def test_a_summary_that_cannot_be_embedded_compacts_nothing(monkeypatch) -> None:
    """Without the summary's vector the compacted turns would leave search for good,
    so nothing is written and the originals stay as they are."""
    import server.core.ch
    import server.llm.client

    class _DownEmbedder:
        model_name = "fake"
        dim = 3

        async def embed(self, text):
            raise RuntimeError("embedder down")

    rows = [
        {
            "msg_id": f"m{i}",
            "role": "user",
            "content": f"turn {i}",
            "ts": T0 + timedelta(seconds=i),
            "metadata": "{}",
            "user_id": "alice",
            "run_id": "run-1",
        }
        for i in range(6)
    ]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(ch_mod, "get_embedder", lambda: _DownEmbedder())

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=2
            )
        )
    )
    assert (out.compacted, out.reason) == (False, "embed-failed")
    assert fake.inserted == [] and fake.embedded == [] and fake.executed == []


def test_a_rerun_on_a_compacted_thread_compacts_nothing(monkeypatch) -> None:
    """The earlier summary sits where the turns it replaced began, so a re-run finds it
    among the candidates: a window of nothing but summaries is already compacted, and
    no LLM call is made."""
    import server.core.ch
    import server.llm.client

    summary = {
        "msg_id": "compact-1",
        "role": "system",
        "content": "a summary",
        "ts": T0,
        "metadata": '{"compacted_from": ["m0", "m1"]}',
        "user_id": "alice",
        "run_id": "run-1",
    }
    kept = [
        {
            "msg_id": f"m{i}",
            "role": "user",
            "content": f"turn {i}",
            "ts": T0 + timedelta(seconds=i),
            "metadata": "{}",
            "user_id": "alice",
            "run_id": "run-1",
        }
        for i in range(2, 4)
    ]
    fake = _FakeCH([summary, *kept])

    class _NoLLM(_FakeLLM):
        async def complete(self, **kw: Any) -> LLMResult:
            raise AssertionError("a re-run must not summarise the summary again")

    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _NoLLM())
    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=2, min_to_compact=1
            )
        )
    )
    assert (out.compacted, out.reason) == (False, "already-compacted")
    assert fake.inserted == [] and fake.embedded == []


def test_a_large_turn_behind_a_summary_is_compacted_whole(monkeypatch) -> None:
    """An earlier summary is folded in without spending the prompt budget, so the turn
    behind it is summarised (whole, up to the cap) rather than leaving a window of
    the summary alone to be compacted again on every run."""
    import server.core.ch
    import server.llm.client

    big = "x" * 15000
    rows = [
        {
            "msg_id": "compact-1",
            "role": "system",
            "content": "s" * 2000,
            "ts": T0,
            "metadata": '{"compacted_from": ["m0"]}',
            "user_id": "alice",
            "run_id": "run-1",
        },
        *(
            {
                "msg_id": f"m{i}",
                "role": "user",
                "content": big if i == 1 else f"turn {i}",
                "ts": T0 + timedelta(seconds=i),
                "metadata": "{}",
                "user_id": "alice",
                "run_id": "run-1",
            }
            for i in range(1, 4)
        ),
    ]
    llm = _FakeLLM()

    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: llm)
    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=1
            )
        )
    )
    assert out.compacted is True and out.messages_summarized == 2  # m1, m2; not the summary
    [summary] = fake.inserted
    # The summary is free, so the 15,000-character turn and the short one after it fit.
    assert '"compacted_from": ["compact-1", "m1", "m2"]' in summary["metadata"]
    [prompt] = llm.prompts
    assert big in prompt  # the large turn reached the summarizer whole


def test_an_earlier_summary_does_not_count_toward_min_to_compact(monkeypatch) -> None:
    """min_to_compact counts new turns: a summary and three turns are below a minimum
    of four, and no LLM call is made."""
    import server.core.ch
    import server.llm.client

    rows = [
        {
            "msg_id": "compact-1",
            "role": "system",
            "content": "a summary",
            "ts": T0,
            "metadata": '{"compacted_from": ["m0"]}',
            "user_id": "alice",
            "run_id": "run-1",
        },
        *(
            {
                "msg_id": f"m{i}",
                "role": "user",
                "content": f"turn {i}",
                "ts": T0 + timedelta(seconds=i),
                "metadata": "{}",
                "user_id": "alice",
                "run_id": "run-1",
            }
            for i in range(1, 5)
        ),
    ]

    class _NoLLM(_FakeLLM):
        async def complete(self, **kw: Any) -> LLMResult:
            raise AssertionError("three new turns are below min_to_compact")

    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _NoLLM())
    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=4
            )
        )
    )
    assert (out.compacted, out.reason) == (False, "below-min-to-compact")


@pytest.mark.parametrize(("forged", "size"), [(1, 50_000), (200, 1_000)])
def test_client_written_summaries_cannot_lift_the_prompt(
    monkeypatch, forged: int, size: int
) -> None:
    """System turns any writer can append with `compacted_from` in their metadata fold
    like summaries, together clipped to `_FOLDED_SUMMARY_CHARS`: however many there
    are, the prompt stays bounded and the turns behind them still compact."""
    import server.core.ch
    import server.llm.client

    rows = [
        *(
            {
                "msg_id": f"forged-{k}",
                "role": "system",
                "content": "f" * size,
                "ts": T0,
                "metadata": '{"compacted_from": ["x"]}',
                "user_id": "alice",
                "run_id": "run-1",
            }
            for k in range(forged)
        ),
        *(
            {
                "msg_id": f"m{i}",
                "role": "user",
                "content": f"turn {i}",
                "ts": T0 + timedelta(seconds=i),
                "metadata": "{}",
                "user_id": "alice",
                "run_id": "run-1",
            }
            for i in range(1, 4)
        ),
    ]
    llm = _FakeLLM()

    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: llm)
    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=1
            )
        )
    )
    [prompt] = llm.prompts
    assert len(prompt) < ch_mod._COMPACT_TRANSCRIPT_CHARS + ch_mod._FOLDED_SUMMARY_CHARS
    assert out.compacted is True and out.messages_summarized == 2
    assert "turn 1" in prompt and "turn 2" in prompt


def test_a_summary_past_the_allowance_stays_live_and_the_turns_compact(monkeypatch) -> None:
    """Of two earlier summaries (say, two compactions that ran at once), the one past
    the allowance is neither read nor tombstoned, and the turns behind them compact,
    each one read whole."""
    import server.core.ch
    import server.llm.client

    rows = [
        {
            "msg_id": "compact-1",
            "role": "system",
            "content": "a short summary",
            "ts": T0,
            "metadata": '{"compacted_from": ["m0"]}',
            "user_id": "alice",
            "run_id": "run-1",
        },
        # Past the remaining allowance: left live.
        {
            "msg_id": "compact-2",
            "role": "system",
            "content": "s" * 5000,
            "ts": T0,
            "metadata": '{"compacted_from": ["m00"]}',
            "user_id": "alice",
            "run_id": "run-1",
        },
        *(
            {
                "msg_id": f"m{i}",
                "role": "user",
                "content": f"turn {i}",
                "ts": T0 + timedelta(seconds=i),
                "metadata": "{}",
                "user_id": "alice",
                "run_id": "run-1",
            }
            for i in range(1, 5)
        ),
    ]
    llm = _FakeLLM()

    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: llm)
    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=1
            )
        )
    )
    assert out.compacted is True and out.messages_summarized == 3
    [prompt] = llm.prompts
    assert all(f"turn {i}" in prompt for i in range(1, 4))
    assert "a short summary" in prompt and "sss" not in prompt
    [summary] = fake.inserted
    assert json.loads(summary["metadata"])["compacted_from"] == [
        "compact-1",
        "m1",
        "m2",
        "m3",
    ]


@pytest.mark.parametrize(
    ("lines", "is_summary", "expected"),
    [
        # Plain turns: as many as fit the budget.
        (["user: a", "user: b"], [False, False], ([0, 1], "user: a\nuser: b")),
        # A summary is free and the window reaches the turn behind it, whole.
        (
            ["system: " + "s" * 100, "user: " + "x" * 15_990],
            [True, False],
            ([0, 1], "system: " + "s" * 100 + "\nuser: " + "x" * 15_990),
        ),
        # A summary past the allowance stays live and out of the prompt, and the turn
        # behind it still compacts.
        (
            ["system: " + "s" * 4_500, "user: " + "x" * 11_000],
            [True, False],
            ([1], "user: " + "x" * 11_000),
        ),
        # A second summary past what is left stays live too; the first folds in whole.
        (
            ["system: " + "s" * 3_500, "system: " + "s" * 1_000, "user: " + "x" * 15_500],
            [True, True, False],
            ([0, 2], "system: " + "s" * 3_500 + "\nuser: " + "x" * 15_500),
        ),
        # A later summary that fits still folds in after one that does not.
        (
            ["system: " + "s" * 4_000, "system: s", "user: a"],
            [True, True, False],
            ([1, 2], "system: s\nuser: a"),
        ),
        # A summary after a turn takes budget like a turn and is replaced with it.
        (
            ["user: a", "system: S", "user: b"],
            [False, True, False],
            ([0, 1, 2], "user: a\nsystem: S\nuser: b"),
        ),
        # Nothing but summaries (the handler answers already-compacted before this).
        (["system: s"], [True], ([0], "system: s")),
    ],
)
def test_the_compaction_window(
    lines: list[str], is_summary: list[bool], expected: tuple[list[int], str]
) -> None:
    assert ch_mod._compaction_window(lines, is_summary, [False] * len(lines)) == expected


def test_a_stale_summary_is_replaced_unread() -> None:
    """A summary whose sources are all still live (its run failed before tombstoning
    them) is replaced without being read or spending the allowance, so a resend leaves
    one summary, and the earlier summary beside it still folds in."""
    lines = ["system: " + "s" * 3_000, "system: " + "t" * 3_000, "user: a"]
    replaced, transcript = ch_mod._compaction_window(
        lines, [True, True, False], [False, True, False]
    )
    assert replaced == [0, 1, 2]
    assert transcript == "system: " + "s" * 3_000 + "\nuser: a"


def test_a_stale_summary_with_a_source_past_the_window_is_read() -> None:
    """A resend whose window stops before one of a stale summary's sources (a larger
    keep_recent) reads that summary, so the source left live keeps its cover."""
    rows = [
        {
            "msg_id": "compact-1",
            "role": "system",
            "content": "t" * 100,
            "metadata": '{"compacted_from": ["m1", "m2", "m3"]}',
            "ts": T0,
        },
        *(
            {"msg_id": f"m{i}", "role": "user", "content": f"turn {i}", "metadata": "{}", "ts": T0}
            for i in (1, 2)
        ),
    ]
    replaced, transcript = ch_mod._window_of(rows, [True, False, False])
    assert replaced == [0, 1, 2] and "ttt" in transcript
    # Every source inside the window: replaced unread.
    rows[0]["metadata"] = '{"compacted_from": ["m1", "m2"]}'
    replaced, transcript = ch_mod._window_of(rows, [True, False, False])
    assert replaced == [0, 1, 2] and "ttt" not in transcript


def test_a_resent_compaction_replaces_the_summary_its_failed_run_left(monkeypatch) -> None:
    """S0 compacted earlier turns; a run then wrote S1 over S0 and m1..m3 and failed
    before tombstoning them. The resend replaces S0, S1 and the turns with one summary,
    reading S0 and the turns but not S1."""
    import server.core.ch
    import server.llm.client

    def row(mid: str, content: str, metadata: str = "{}", role: str = "user") -> dict[str, Any]:
        return {
            "msg_id": mid,
            "role": role,
            "content": content,
            "ts": T0,
            "metadata": metadata,
            "user_id": "alice",
            "run_id": "run-1",
        }

    rows = [
        row("compact-0", "s" * 3_000, '{"compacted_from": ["m0"]}', "system"),
        row(
            "compact-1",
            "t" * 3_000,
            '{"compacted_from": ["compact-0", "m1", "m2", "m3"]}',
            "system",
        ),
        *(row(f"m{i}", f"turn {i}") for i in range(1, 5)),
    ]
    llm = _FakeLLM()

    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: llm)
    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=1
            )
        )
    )
    assert out.compacted is True and out.messages_summarized == 3
    [prompt] = llm.prompts
    assert "sss" in prompt and "ttt" not in prompt
    [summary] = fake.inserted
    assert json.loads(summary["metadata"])["compacted_from"] == [
        "compact-0",
        "compact-1",
        "m1",
        "m2",
        "m3",
    ]


@pytest.mark.parametrize(
    ("role", "metadata", "summary"),
    [
        ("system", '{"compacted_from": ["m1"]}', True),
        ("system", '{"compacted_from": []}', False),
        ("system", '{"compacted_from": "m1"}', False),
        ("system", "{}", False),
        ("assistant", '{"compacted_from": ["m1"]}', False),
    ],
)
def test_only_a_system_turn_with_a_compacted_from_list_is_a_summary(
    role: str, metadata: str, summary: bool
) -> None:
    """The same test `_PEER_COPIES` applies in SQL: a system turn with a non-empty
    `compacted_from` list."""
    assert bool(ch_mod._compacted_from({"role": role, "metadata": metadata})) is summary


def test_a_summary_after_its_first_source_is_not_stale() -> None:
    """Only a summary at the earliest ts of its sources is what a failed run leaves. A
    client summary appended later over a live summary (rows in live_messages order:
    s1, then s2 naming s1, then a turn) is read like any other summary."""

    def row(mid: str, ts: datetime, metadata: str, role: str = "system") -> dict[str, Any]:
        return {"msg_id": mid, "role": role, "content": mid * 10, "ts": ts, "metadata": metadata}

    s1 = row("s1", T0, '{"compacted_from": ["m0"]}')
    turn = row("m1", T0 + timedelta(seconds=2), "{}", "user")
    for s2_ts, read in ((T0 + timedelta(seconds=1), True), (T0, False)):
        rows = [s1, row("s2", s2_ts, '{"compacted_from": ["s1"]}'), turn]
        _, transcript = ch_mod._window_of(rows, [True, True, False])
        assert ("s2s2" in transcript) is read


@pytest.mark.parametrize(("min_to_compact", "compacted"), [(2, True), (3, False)])
def test_a_summary_inside_the_window_is_replaced_but_not_counted(
    monkeypatch, min_to_compact: int, compacted: bool
) -> None:
    """A client summary appended between turns is read and replaced with them, and
    counts toward neither min_to_compact nor messages_summarized."""
    import server.core.ch
    import server.llm.client

    def row(mid: str, i: int, content: str, metadata: str = "{}", role: str = "user"):
        return {
            "msg_id": mid,
            "role": role,
            "content": content,
            "ts": T0 + timedelta(seconds=i),
            "metadata": metadata,
            "user_id": "alice",
            "run_id": "run-1",
        }

    rows = [
        row("m1", 1, "turn 1"),
        row("s", 2, "a client summary", '{"compacted_from": ["m0"]}', "system"),
        row("m2", 3, "turn 2"),
        row("m3", 4, "turn 3"),
    ]
    llm = _FakeLLM()

    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: llm)
    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o",
                namespace="n",
                agent="a",
                thread_id="t",
                keep_recent=1,
                min_to_compact=min_to_compact,
            )
        )
    )
    assert out.compacted is compacted
    if compacted:
        [prompt] = llm.prompts
        assert "a client summary" in prompt
        assert out.messages_summarized == 2
        [summary] = fake.inserted
        assert json.loads(summary["metadata"])["compacted_from"] == ["m1", "m2", "s"]
    else:
        assert out.reason == "below-min-to-compact" and llm.prompts == []


def test_a_summary_that_reads_as_a_credential_compacts_nothing(monkeypatch) -> None:
    """A summary that repeats a key pasted in one turn would have no vector, so search
    would lose the window for good: nothing is written and no turn is tombstoned."""
    import server.core.ch
    import server.llm.client

    class _KeyLLM(_FakeLLM):
        async def complete(self, **kw: Any) -> LLMResult:
            self.prompts.append(kw["user"])
            return LLMResult(
                text=f"The user shared {OPENAI_KEY} and planned the week.",
                tokens_in=1,
                tokens_out=2,
                provider="fake",
                model="fake",
            )

    class _NoEmbedder:
        model_name = "fake"
        dim = 3

        async def embed(self, text):
            raise AssertionError("a credential summary must not reach the embedder")

    rows = [_turn(i, secret=i == 2, run_id="run-1") for i in range(6)]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _KeyLLM())
    monkeypatch.setattr(ch_mod, "get_embedder", lambda: _NoEmbedder())

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=2
            )
        )
    )
    assert (out.compacted, out.reason) == (False, "summary-reads-as-credential")
    assert fake.inserted == [] and fake.embedded == [] and fake.executed == []


def test_a_credential_turn_is_left_out_of_the_window_and_stays_live(monkeypatch) -> None:
    """The summarizer never sees a turn that reads as a credential, even one that echoes
    its input; the turn stays live, and the rest of the window compacts."""
    import server.core.ch
    import server.llm.client

    class _EchoLLM(_FakeLLM):
        async def complete(self, **kw: Any) -> LLMResult:
            self.prompts.append(kw["user"])
            return LLMResult(
                text=kw["user"][:200], tokens_in=1, tokens_out=2, provider="fake", model="fake"
            )

    rows = [_turn(i, secret=i == 1, run_id="run-1") for i in range(6)]
    fake = _FakeCH(rows)
    llm = _EchoLLM()
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: llm)

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=2
            )
        )
    )
    assert out.compacted is True and out.messages_summarized == 4
    assert all(OPENAI_KEY not in prompt for prompt in llm.prompts)
    [summary] = fake.inserted
    assert '"compacted_from": ["m0", "m2", "m3", "m4"]' in summary["metadata"]
    tombstoned = [
        params["ids"] for sql, params in fake.executed if "ALTER TABLE loom.conversations\n" in sql
    ]
    assert tombstoned == [["m0", "m2", "m3", "m4"]]


def test_compact_never_summarizes_or_tombstones_a_saved_turn(monkeypatch) -> None:
    """A message the user saved for search keeps its row: compaction folds the
    turns around it and leaves it verbatim."""
    import server.core.ch
    import server.llm.client

    rows = [
        {
            "msg_id": f"m{i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}",
            "ts": T0 + timedelta(seconds=i),
            "metadata": "{}",
            "user_id": "alice",
            "run_id": "-",
            "is_saved": i == 1,
        }
        for i in range(8)
    ]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _FakeLLM())

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=2, min_to_compact=2
            )
        )
    )
    assert out.compacted is True
    assert out.messages_summarized == 5
    [summary] = fake.inserted
    import json

    assert "m1" not in json.loads(summary["metadata"])["compacted_from"]


def test_a_saved_and_a_credential_turn_both_stay_live(monkeypatch) -> None:
    """Both filters hold together: the saved turn and the credential turn are left out of
    the window, never sent to the summarizer and never tombstoned."""
    import server.core.ch
    import server.llm.client

    rows = [_turn(i, secret=i == 2, saved=i == 1) for i in range(7)]
    fake = _FakeCH(rows)
    llm = _FakeLLM()
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: llm)

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=2
            )
        )
    )
    assert out.compacted is True and out.messages_summarized == 4
    assert all(OPENAI_KEY not in prompt and "turn 1" not in prompt for prompt in llm.prompts)
    tombstoned = [
        params["ids"] for sql, params in fake.executed if "ALTER TABLE loom.conversations\n" in sql
    ]
    assert tombstoned == [["m0", "m3", "m4", "m5"]]


@pytest.mark.parametrize("held", ["saved", "credential"])
def test_a_window_of_only_held_turns_has_nothing_to_compact(monkeypatch, held) -> None:
    """Every turn before the recent ones is saved, or reads as a credential: none can be
    summarised, so nothing is (not `already-compacted`: there is no summary)."""
    import server.core.ch
    import server.llm.client

    rows = [
        _turn(i, secret=held == "credential" and i < 4, saved=held == "saved" and i < 4)
        for i in range(5)
    ]
    fake = _FakeCH(rows)
    llm = _FakeLLM()
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: llm)
    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1, min_to_compact=2
            )
        )
    )
    assert (out.compacted, out.reason) == (False, "nothing-to-compact")
    assert llm.prompts == [] and fake.inserted == [] and fake.executed == []


def test_long_turns_compact_and_the_check_stops_past_the_window(monkeypatch) -> None:
    """Turns longer than a third of the window still compact at the default
    min_to_compact: the credential check runs until the window is full and enough new
    turns are counted, and never reaches a turn past that cut."""
    import server.core.ch
    import server.llm.client

    rows = [
        {**_turn(i), "role": "assistant", "content": f"turn {i} " + "x" * 6990} for i in range(6)
    ]
    rows += [_turn(6, secret=True), _turn(7)]
    fake = _FakeCH(rows)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(server.llm.client, "get_llm_client", lambda: _FakeLLM())
    checked: list[str] = []
    reads_high = ch_mod.reads_high

    def recording_reads_high(text: str) -> bool:
        checked.append(text)
        return reads_high(text)

    monkeypatch.setattr(ch_mod, "reads_high", recording_reads_high)

    out = asyncio.run(
        ch_mod.handle_conversation_compact(
            ConversationCompactInput(
                org="o", namespace="n", agent="a", thread_id="t", keep_recent=1
            )
        )
    )
    assert out.compacted is True and out.messages_summarized == 2
    assert all(OPENAI_KEY not in text for text in checked)
