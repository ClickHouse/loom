"""memory.ask deep-evidence pass: escalate the evidence, not
only the model.

Retrieval, the LLM and the evidence read are all faked. The selector call is
told apart from a synthesis call by its system prompt, so one scripted client
serves both.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit
from server.memory.app import deep_evidence as deep
from server.memory.app.deep_evidence import (
    EvidenceTurn,
    candidate_lines,
    fetch_raw_evidence,
    join_links_to_evidence,
    parse_picks,
    render_raw_evidence,
)


class _ScriptedLLM:
    """Selector replies with `picks`; synthesis replies come from `answers` in
    order (the last one repeats)."""

    provider = "fake"
    model = "fake-llm"

    def __init__(self, answers: list[str], picks: str = '{"picks": [0]}') -> None:
        self._answers = list(answers)
        self._picks = picks
        self.calls: list[tuple[str, str]] = []
        self.synth_calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        if "worth opening" in system:
            return SimpleNamespace(text=self._picks)
        self.synth_calls.append((system, user))
        text = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        return SimpleNamespace(text=text)


def _hit(key: str, excerpt: str, sim: float = 0.8) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=1 - sim,
        similarity=sim,
        rank_score=sim,
    )


def _output(hits: list[_MemorySearchHit]) -> MemorySearchOutput:
    return MemorySearchOutput(results=hits, embedding_model="fake-embed")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace()
    e.llm = _ScriptedLLM(["Blue [m1]."])
    e.search_output = _output(
        [
            _hit("m1", "User's favorite color is blue."),
            _hit("m2", "User's favorite food is sushi.", 0.6),
        ]
    )
    e.turns = [
        EvidenceTurn(
            "m1",
            "user",
            "2026-01-02 10:00:00",
            "my favourite colour is cobalt blue, not navy",
            "extracted_from",
        ),
    ]
    e.fetch_calls: list[dict[str, Any]] = []

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return e.search_output

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    async def fake_fetch(**kw: Any) -> list[EvidenceTurn]:
        e.fetch_calls.append(kw)
        return list(e.turns)

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(deep, "fetch_raw_evidence", fake_fetch)

    def set_llm(llm: Any) -> None:
        e.llm = llm
        monkeypatch.setattr(llm_client, "get_llm_client", lambda: llm)

    e.set_llm = set_llm
    e.monkeypatch = monkeypatch
    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    e.settings = settings
    return e


async def _run(**overrides: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "what is my favorite color",
        # These tests script the LLM in the serial order (excerpt synthesis,
        # selector, deep synthesis); the deep-first order is pinned in
        # test_memory_ask_deep_first.py.
        "deep_first": False,
    }
    base.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**base))


# ---- the pass runs only when asked for (or on a temporal question) ---------


async def test_auto_answers_from_excerpts_only(env) -> None:
    out = await _run()
    assert out.source == "synthesized"
    assert out.depth == "excerpt"
    assert out.deep_fired is False
    assert out.deep_reason == ""
    assert len(env.llm.calls) == 1
    assert env.fetch_calls == []


async def test_depth_raw_forces_the_pass(env) -> None:
    env.set_llm(_ScriptedLLM(["Blue [m1].", "Cobalt blue, not navy [m1]."]))
    out = await _run(depth="raw")
    assert out.deep_fired is True
    assert out.deep_reason == "requested"
    assert out.depth == "raw"
    assert out.answer == "Cobalt blue, not navy [m1]."
    assert out.deep_keys == ["m1"]
    assert out.deep_chars > 0
    assert out.memory_keys == ["m1"]
    # excerpt synth, selector, deep synth
    assert len(env.llm.calls) == 3
    # The deep synthesis saw the raw turn, labelled with its memory key.
    system, user = env.llm.synth_calls[-1]
    assert "Source turns behind the memories" in user
    assert "[m1] (user) my favourite colour is cobalt blue" in user
    assert "PREFER THE TURN'S EXACT WORDING" in system
    # The evidence read was scoped to the picked key with the configured caps.
    assert env.fetch_calls[0]["memory_keys"] == ["m1"]
    assert env.fetch_calls[0]["max_chars_total"] == env.settings.memory_ask_deep_evidence_max_chars


async def test_depth_excerpt_pins_excerpt_on_a_hedged_answer(env) -> None:
    env.llm._answers = ["I have no memory specifically about that."]
    out = await _run(depth="excerpt")
    assert out.deep_fired is False
    assert out.depth == "excerpt"
    assert len(env.llm.calls) == 1


# ---- auto: a failed citation contract is not a trigger ----


async def test_auto_hedged_answer_does_not_open_evidence(env) -> None:
    """There is no citation-failure trigger: on auto a hedged or uncited
    excerpt answer is served as it is."""
    for first in ("I have no memory specifically about that [m1].", "Probably blue."):
        env.set_llm(_ScriptedLLM([first, "Cobalt blue [m1]."]))
        out = await _run()
        assert out.deep_fired is False and out.deep_reason == ""
        assert out.depth == "excerpt"
        assert env.fetch_calls == []


async def test_deep_answer_that_is_worse_is_discarded(env, monkeypatch) -> None:
    """The raw read must not make the answer hedge where the excerpts did not."""
    env.set_llm(_ScriptedLLM(["Blue [m1].", "I cannot determine that from the memories."]))
    out = await _run(depth="raw")
    assert out.deep_fired is True
    assert out.depth == "excerpt"
    assert out.answer == "Blue [m1]."


async def test_empty_evidence_block_keeps_the_excerpt_answer(env) -> None:
    env.turns = []
    out = await _run(depth="raw")
    assert out.deep_fired is True
    assert out.deep_chars == 0
    assert out.depth == "excerpt"
    assert out.answer == "Blue [m1]."
    # No second synthesis when there was nothing to add.
    assert len(env.llm.synth_calls) == 1


async def test_selector_garbage_falls_back_to_top_ranked(env) -> None:
    env.set_llm(_ScriptedLLM(["Blue [m1].", "Blue [m1]."], picks="not json at all"))
    out = await _run(depth="raw")
    # Top ranked = m1 first; the fallback still opens something.
    assert env.fetch_calls[0]["memory_keys"][0] == "m1"
    assert out.deep_fired is True


# ---- pure helpers ------------------------------------------------------------


def test_parse_picks_is_in_range_deduplicated_and_capped() -> None:
    assert parse_picks('{"picks": [2, 0, 2, 9, "1", -1]}', 5, max_picks=2) == [2, 0]
    assert parse_picks('prose then {"picks": [1]} trailing', 3, max_picks=5) == [1]
    assert parse_picks("", 3, max_picks=5) == []
    assert parse_picks('{"picks": "1"}', 3, max_picks=5) == []


def test_candidate_lines_are_numbered_and_compact() -> None:
    lines = candidate_lines([_hit("k1", "x " * 500), _hit("k2", "short")]).split("\n")
    assert lines[0].startswith("[0] ") and " k1: " in lines[0]
    assert len(lines[0]) < 240
    assert lines[1].endswith("k2: short")


def test_render_raw_evidence_labels_each_turn() -> None:
    block = render_raw_evidence(
        [
            EvidenceTurn("m1", "user", "2026-01-02 10:00:00", "hello", "extracted_from"),
            EvidenceTurn("m1", "", "", "again", "window"),
        ]
    )
    assert block.split("\n\n") == ["[2026-01-02] [m1] (user) hello", "[undated] [m1] (turn) again"]


class _FakeCH:
    """Answers the two keyed reads of `fetch_raw_evidence`
    from one list of joined rows: the first call gets the link rows, the
    second the evidence rows, one evidence id per joined row."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.params: dict[str, Any] | None = None  # the links read's params
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        if len(self.calls) == 1:
            self.params = params
            return [
                {
                    "memory_key": r["memory_key"],
                    "support_type": r["support_type"],
                    "char_start": 0,
                    "char_end": 10 if r["scoped_span"] else 0,
                    "evidence_id": f"ev-{i}",
                }
                for i, r in enumerate(self.rows)
            ]
        return [
            {
                "evidence_id": f"ev-{i}",
                "role": r["role"],
                "observed_at": r["observed_at"],
                "content": r["content"],
            }
            for i, r in enumerate(self.rows)
        ]


def _row(key: str, support: str, scoped: bool, content: str, ts: str = "2026-01-01 00:00:00"):
    return {
        "memory_key": key,
        "support_type": support,
        "scoped_span": scoped,
        "role": "user",
        "observed_at": ts,
        "content": content,
    }


async def test_fetch_prefers_scoped_rows_and_skips_the_window_when_scoped_exist() -> None:
    ch = _FakeCH(
        [
            _row("m1", "extracted_from", True, "the scoped turn"),
            _row("m1", "extracted_from", False, "window turn one"),
            _row("m1", "extracted_from", False, "window turn two"),
        ]
    )
    turns = await fetch_raw_evidence(
        org="o",
        namespace="ns",
        memory_keys=["m1"],
        max_chars_per_memory=1000,
        max_chars_total=5000,
        ch=ch,
    )
    assert [t.content for t in turns] == ["the scoped turn"]
    assert ch.params == {"org": "o", "ns": "ns", "keys": ["m1"]}
    # Two keyed reads, neither FINAL: links by key, then evidence by id.
    assert len(ch.calls) == 2
    assert "FINAL" not in ch.calls[0][0] and "FINAL" not in ch.calls[1][0]
    assert "LIMIT 1 BY memory_key, evidence_id" in ch.calls[0][0]
    assert "LIMIT 1 BY evidence_id" in ch.calls[1][0]
    assert ch.calls[1][1] == {"org": "o", "ns": "ns", "ids": ["ev-0", "ev-1", "ev-2"]}


async def test_fetch_skips_the_evidence_read_when_no_link_is_live() -> None:
    class _NoLinks(_FakeCH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            self.calls.append((sql, params or {}))
            return []

    ch = _NoLinks([])
    assert (
        await fetch_raw_evidence(
            org="o",
            namespace="ns",
            memory_keys=["m1"],
            max_chars_per_memory=10,
            max_chars_total=10,
            ch=ch,
        )
        == []
    )
    assert len(ch.calls) == 1


def test_join_orders_like_the_final_join_did_and_drops_dead_or_empty_evidence() -> None:
    """The one-statement shape ordered by memory key, scoped support types
    first, a real span before (0,0), then oldest first, and capped at 400
    rows; the Python join reproduces that on the two reads' rows."""
    links = [
        {
            "memory_key": "m2",
            "support_type": "window",
            "char_start": 0,
            "char_end": 0,
            "evidence_id": "e4",
        },
        {
            "memory_key": "m1",
            "support_type": "extracted_from",
            "char_start": 0,
            "char_end": 0,
            "evidence_id": "e2",
        },
        {
            "memory_key": "m1",
            "support_type": "extracted_from",
            "char_start": 5,
            "char_end": 40,
            "evidence_id": "e3",
        },
        {
            "memory_key": "m1",
            "support_type": "extracted_from",
            "char_start": 3,
            "char_end": 30,
            "evidence_id": "e1",
        },
        {
            "memory_key": "m1",
            "support_type": "window",
            "char_start": 0,
            "char_end": 0,
            "evidence_id": "e5",
        },
        {
            "memory_key": "m1",
            "support_type": "extracted_from",
            "char_start": 3,
            "char_end": 30,
            "evidence_id": "gone",
        },
    ]
    evidence = [
        {"evidence_id": "e1", "role": "user", "observed_at": "2026-01-01 00:00:02", "content": "b"},
        {"evidence_id": "e2", "role": "user", "observed_at": "2026-01-01 00:00:00", "content": "w"},
        {"evidence_id": "e3", "role": "user", "observed_at": "2026-01-01 00:00:01", "content": "a"},
        {
            "evidence_id": "e4",
            "role": "user",
            "observed_at": "2026-01-01 00:00:00",
            "content": "m2",
        },
        {"evidence_id": "e5", "role": "user", "observed_at": "2026-01-01 00:00:03", "content": "z"},
    ]
    out = join_links_to_evidence(links, evidence)
    assert [(r["memory_key"], r["content"]) for r in out] == [
        ("m1", "a"),  # scoped, oldest
        ("m1", "b"),  # scoped
        ("m1", "w"),  # extracted_from but a (0,0) span
        ("m1", "z"),  # window support type
        ("m2", "m2"),
    ]
    assert out[0]["scoped_span"] is True and out[2]["scoped_span"] is False
    assert join_links_to_evidence(links, evidence, cap=2) == out[:2]


async def test_fetch_opens_the_window_only_when_nothing_is_scoped_and_caps_it() -> None:
    ch = _FakeCH(
        [
            _row("m1", "extracted_from", False, "a" * 400, "2026-01-01 00:00:01"),
            _row("m1", "extracted_from", False, "b" * 400, "2026-01-01 00:00:02"),
            _row("m1", "extracted_from", False, "c" * 400, "2026-01-01 00:00:03"),
            _row("m2", "lexical_support", True, "d" * 100),
        ]
    )
    turns = await fetch_raw_evidence(
        org="o",
        namespace="ns",
        memory_keys=["m1", "m2"],
        max_chars_per_memory=500,
        max_chars_total=560,
        ch=ch,
    )
    per_key = {}
    for t in turns:
        per_key[t.memory_key] = per_key.get(t.memory_key, 0) + len(t.content)
    assert per_key["m1"] <= 500
    assert sum(per_key.values()) <= 560
    # Chronological across memories.
    assert [t.observed_at for t in turns] == sorted(t.observed_at for t in turns)


async def test_fetch_is_fail_open() -> None:
    class _Boom:
        async def query_all(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("ch down")

    assert (
        await fetch_raw_evidence(
            org="o",
            namespace="ns",
            memory_keys=["m1"],
            max_chars_per_memory=10,
            max_chars_total=10,
            ch=_Boom(),
        )
        == []
    )


async def test_fetch_with_no_keys_does_not_query() -> None:
    ch = _FakeCH([_row("m1", "extracted_from", True, "x")])
    assert (
        await fetch_raw_evidence(
            org="o",
            namespace="ns",
            memory_keys=[],
            max_chars_per_memory=10,
            max_chars_total=10,
            ch=ch,
        )
        == []
    )
    assert ch.params is None


# ---- session-summary hits open their session, windowed by the question ------

from server.memory.app.deep_evidence import (  # noqa: E402
    fetch_session_turns,
    session_ids_of,
    window_turns_by_question,
)


def test_session_ids_of_reads_summary_keys_only() -> None:
    assert session_ids_of(["m1", "summary:s1", "summary:s1", "summary:", "summary:s2"]) == [
        "s1",
        "s2",
    ]


def _turn(i: int, content: str) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "role": "user",
        "observed_at": f"2026-01-01 00:00:{i:02d}",
        "content": content,
    }


def test_window_keeps_scoring_turns_with_neighbours_in_order() -> None:
    rows = [
        _turn(0, "hello there"),
        _turn(1, "we booked the Denver trip for March"),
        _turn(2, "great, bring a jacket"),
        _turn(3, "unrelated chatter about lunch"),
        _turn(4, "also the concert in Denver was Brandon Flowers"),
        _turn(5, "nice"),
    ]
    kept = window_turns_by_question(rows, "What did I do in Denver?", max_chars=10_000)
    idx = [rows.index(r) for r in kept]
    assert idx == [0, 1, 2, 3, 4, 5] or idx == sorted(idx)  # chronological
    assert 1 in idx and 4 in idx  # both scoring turns
    assert 3 in idx  # neighbour of turn 4


def test_window_respects_the_character_cap() -> None:
    rows = [_turn(i, f"Denver turn {i} " + "x" * 200) for i in range(10)]
    kept = window_turns_by_question(rows, "Denver", max_chars=700)
    assert 0 < len(kept) <= 3
    assert sum(len(r["content"]) for r in kept) <= 700


def test_window_returns_nothing_without_a_scoring_turn() -> None:
    rows = [_turn(0, "hello"), _turn(1, "goodbye")]
    assert window_turns_by_question(rows, "Denver trip", max_chars=1000) == []


class _OneReadCH:
    """A single statement answered with the rows given (the session-turn read)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.params: dict[str, Any] | None = None

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.params = params
        return self.rows


async def test_fetch_session_turns_labels_with_the_summary_key_and_caps() -> None:
    ch = _OneReadCH(
        [
            {
                "session_id": "s1",
                "role": "user",
                "observed_at": "2026-01-01 00:00:01",
                "content": "Denver plan " + "a" * 100,
            },
            {
                "session_id": "s1",
                "role": "assistant",
                "observed_at": "2026-01-01 00:00:02",
                "content": "ok",
            },
            {
                "session_id": "s2",
                "role": "user",
                "observed_at": "2026-01-02 00:00:01",
                "content": "Denver again " + "b" * 100,
            },
        ]
    )
    turns = await fetch_session_turns(
        org="o",
        namespace="ns",
        session_ids=["s1", "s2"],
        question="Denver",
        max_chars_per_session=1000,
        max_chars_total=150,
        ch=ch,
    )
    assert ch.params["sids"] == ["s1", "s2"]
    assert turns and turns[0].memory_key == "summary:s1" and turns[0].support_type == "session"
    # 150 total: s1's window (114 + 2 chars) fits, s2 does not.
    assert [t.memory_key for t in turns] == ["summary:s1", "summary:s1"]


async def test_deep_pass_opens_a_picked_session_summary(env) -> None:
    """End to end through memory.ask: the selector picks the summary hit, its
    session is fetched and rendered under the summary key."""
    env.search_output = _output(
        [
            _hit("summary:s9", "Session about the Denver trip and a concert.", 0.9),
            _hit("m1", "User's favorite color is blue.", 0.8),
        ]
    )
    env.set_llm(
        _ScriptedLLM(
            ["Blue [m1].", "Brandon Flowers in Denver [summary:s9]."], picks='{"picks": [0]}'
        )
    )

    async def fake_sessions(**kw: Any) -> list[EvidenceTurn]:
        env.session_calls = kw
        return [
            EvidenceTurn(
                "summary:s9",
                "user",
                "2026-01-02 10:00:00",
                "saw Brandon Flowers in Denver",
                "session",
            )
        ]

    env.monkeypatch.setattr(deep, "fetch_session_turns", fake_sessions)
    out = await _run(query="who did I see in Denver", depth="raw")
    assert env.session_calls["session_ids"] == ["s9"]
    assert env.session_calls["question"] == "who did I see in Denver"
    assert env.fetch_calls[0]["memory_keys"] == []  # no fact keys were picked
    assert out.depth == "raw"
    assert "[summary:s9] (user) saw Brandon Flowers" in env.llm.synth_calls[-1][1]


# ---- the temporal trigger's selector is a setting ----


async def test_temporal_trigger_uses_the_configured_selector(env, monkeypatch) -> None:
    from server.settings import settings

    calls: list[dict[str, Any]] = []

    async def fake_pass(**kw: Any) -> Any:
        calls.append(kw)
        return deep.DeepEvidence()  # empty pass: the excerpt answer stands

    monkeypatch.setattr("server.memory.app.deep_evidence.deep_evidence_pass", fake_pass)
    env.set_llm(_ScriptedLLM(["On 2023-05-20 [rafting]."]))
    await _run(query="When did I go rafting?", decompose="off")
    assert (
        calls and calls[-1]["llm"] is not None
    )  # default selector "llm": the model picks the memories to open
    calls.clear()
    monkeypatch.setattr(settings, "memory_ask_temporal_deep_select", "top")
    env.set_llm(_ScriptedLLM(["On 2023-05-20 [rafting]."]))
    await _run(query="When did I go rafting?", decompose="off")
    assert calls and calls[-1]["llm"] is None  # "top": no selection call
    calls.clear()
    env.set_llm(_ScriptedLLM(["On 2023-05-20 [rafting]."]))
    await _run(query="When did I go rafting?", decompose="off", deep_select="llm")
    assert calls and calls[-1]["llm"] is not None  # per-request override wins
