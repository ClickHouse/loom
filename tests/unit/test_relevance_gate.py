"""memory.ask window cuts: the similarity knee, the proof that ask makes no
judge call, and the synthesis legs that share this file's fixture. The bench
judge's tests live in bench/test_relevance_gate.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit

# ---- through memory.ask ---------------------------------------------------


class _FakeLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text=self._replies.pop(0) if self._replies else "")


def _hit(key: str, excerpt: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        rank_score=0.8,
    )


@pytest.fixture
def ask_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # The temporal raw pass is on by default; these
    # tests script the LLM replies for other mechanisms, so pin it off here.

    monkeypatch.setattr("server.memory.app.ask.pool.looks_temporal", lambda *a, **k: False)
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    env = SimpleNamespace()
    env.hits = [
        _hit("m1", "favorite color is blue"),
        _hit("m2", "bought a blue car in 2019"),
        _hit("m3", "sister's favorite color is green"),
        _hit("m4", "favorite food is sushi"),
        _hit("m5", "repainted the fence blue"),
    ]
    env.llm = _FakeLLM(["Your favorite color is blue [m1]."])

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return MemorySearchOutput(results=env.hits, embedding_model="fake-embed")

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: env.llm)
    monkeypatch.setattr(settings, "memory_ask_relevance_gate_min_keep", 2)
    monkeypatch.setattr(settings, "memory_ask_identifier_injection", False)
    return env


async def _run(**overrides: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "what is my favorite color",
    }
    base.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**base))


async def test_ask_makes_no_judge_call_and_reads_every_row(ask_env) -> None:
    out = await _run()
    assert out.source == "synthesized"
    assert out.gate_mode == "" and out.gate_kept == 0 and out.gate_dropped == 0
    # Exactly one LLM call: the synthesis. Every row reached it.
    assert len(ask_env.llm.calls) == 1
    assert "[m4]" in ask_env.llm.calls[0][1]


# ---- similarity knee ---------------------------------------

from server.memory.domain.relevance_gate import similarity_floor_cut  # noqa: E402


def _sim_rows(sims: list[float]) -> list[Any]:
    return [
        SimpleNamespace(memory_key=f"m{i + 1}", similarity=s, content_excerpt=f"r{i + 1}")
        for i, s in enumerate(sims)
    ]


def test_knee_keeps_rows_near_the_top_and_preserves_order() -> None:
    # A typical shape: a tight on-topic cluster, then a tail far below it.
    rows = _sim_rows([0.366, 0.358, 0.339, 0.330, 0.320, 0.271, 0.221, 0.155, 0.106, 0.053])
    v = similarity_floor_cut(rows, rel_floor=0.75, min_keep=3)
    assert v.mode == "knee"
    assert [m.memory_key for m in v.kept] == ["m1", "m2", "m3", "m4", "m5"]  # >= 0.2745
    assert v.dropped_keys == ["m6", "m7", "m8", "m9", "m10"]


def test_knee_is_membership_not_order() -> None:
    # A reranker put a lower-cosine row first; the cut keeps it in place.
    rows = _sim_rows([0.30, 0.34, 0.10, 0.33, 0.09])
    v = similarity_floor_cut(rows, rel_floor=0.75, min_keep=2)
    assert [m.memory_key for m in v.kept] == ["m1", "m2", "m4"]


def test_knee_floor_tops_up_by_rank() -> None:
    rows = _sim_rows([0.34, 0.32, 0.20, 0.16, 0.13])
    v = similarity_floor_cut(rows, rel_floor=0.75, min_keep=3)
    assert v.mode == "floor" and [m.memory_key for m in v.kept] == ["m1", "m2", "m3"]


def test_knee_off_small_window_and_flat_pool() -> None:
    rows = _sim_rows([0.5, 0.4, 0.3, 0.2])
    assert similarity_floor_cut(rows, rel_floor=0.0, min_keep=3).mode == "skipped"
    assert similarity_floor_cut(rows[:3], rel_floor=0.75, min_keep=3).mode == "skipped"
    flat = similarity_floor_cut(_sim_rows([0.5, 0.49, 0.48, 0.47]), rel_floor=0.75, min_keep=3)
    assert flat.mode == "kept_all" and flat.dropped == []
    zero = similarity_floor_cut(_sim_rows([0.0, 0.0, 0.0, 0.0]), rel_floor=0.75, min_keep=1)
    assert zero.mode == "kept_all"


def test_knee_reads_dict_hits_and_distance_fallback() -> None:
    rows = [
        {"memory_key": "a", "similarity": 0.5},
        {"memory_key": "b", "distance": 0.9},
        {"memory_key": "c", "similarity": 0.45},
        {"memory_key": "d", "distance": 0.95},
    ]
    v = similarity_floor_cut(rows, rel_floor=0.75, min_keep=1)
    assert [m["memory_key"] for m in v.kept] == ["a", "c"]


async def test_window_rel_floor_through_ask(ask_env, monkeypatch) -> None:
    # Hits carry similarity 0.8 each in the fixture; vary them.
    for h, s in zip(ask_env.hits, [0.80, 0.78, 0.40, 0.35, 0.30], strict=True):
        h.similarity = s
    ask_env.llm = _FakeLLM(["Your favorite color is blue [m1]."])
    out = await _run(window_rel_floor=0.75)
    assert out.gate_mode == "knee"  # two rows clear 0.6; the fixture's floor is 2
    assert (out.gate_kept, out.gate_dropped) == (2, 3)
    synth_user = ask_env.llm.calls[0][1]
    assert "[m2]" in synth_user and "[m3]" not in synth_user and "[m5]" not in synth_user
    # Unset or explicit 0 leaves the window uncut.
    ask_env.llm = _FakeLLM(["Blue [m1]."])
    out = await _run(window_rel_floor=0.0)
    assert out.gate_mode == "" and "[m5]" in ask_env.llm.calls[0][1]
    ask_env.llm = _FakeLLM(["Blue [m1]."])
    out = await _run()
    assert out.gate_mode == "" and "[m5]" in ask_env.llm.calls[0][1]


# ---- loss-analysis follow-ups in memory.ask ---------------------------------


async def test_count_question_widens_the_synthesis_window(ask_env, monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_count_window", 30)
    ask_env.hits = [_hit(f"m{i}", f"heirloom number {i} from a relative") for i in range(1, 21)]
    ask_env.llm = _FakeLLM(["ANSWER: 20 [m1]"])
    out = await _run(query="How many antique items did I inherit from my family members?")
    assert out.source == "synthesized"
    assert "[m20]" in ask_env.llm.calls[0][1]  # all 20 reached the synthesizer
    # A non-count question keeps the default 15-row window.
    ask_env.llm = _FakeLLM(["Blue [m1]."])
    await _run(query="what is my favorite color")
    assert "[m16]" not in ask_env.llm.calls[0][1] and "[m15]" in ask_env.llm.calls[0][1]


async def test_resolved_relative_date_is_told_to_the_synthesizer(ask_env) -> None:
    ask_env.llm = _FakeLLM(["No, alone [m1]."])
    await _run(
        query="I mentioned visiting a museum two months ago. Did I visit with a friend or not?",
        observation_date="2023-03-11",
    )
    system = ask_env.llm.calls[0][0]
    assert "resolves to about 2023-01-1" in system  # ~2 months before 2023-03-11


# ---- GitHub-items fallback on abstain ------------------------


async def _abstaining_env(
    ask_env, monkeypatch, items: dict[int, dict[str, Any]], code_ns: str = "code-x"
):
    from server.code.store.index import CodeRepo
    from server.memory.app import code_grounding as mcg

    ask_env.hits = []  # retrieval finds nothing → the abstain branch
    calls: list[tuple[str, str, list[int]]] = []

    async def fake_items(self, org: str, ns: str, numbers: list[int]) -> dict[int, dict[str, Any]]:
        calls.append((org, ns, list(numbers)))
        return {n: dict(items[n]) for n in numbers if n in items}

    monkeypatch.setattr(CodeRepo, "items_by_number", fake_items)

    async def fake_prs(self, org, ns, numbers, **k):  # No PR-history rows here
        return {}

    monkeypatch.setattr(CodeRepo, "prs_by_number", fake_prs)
    monkeypatch.setattr(mcg, "_resolve_code_target", lambda si, st: ("clickhouse", code_ns))
    return calls


async def test_gh_items_fallback_answers_a_known_pr_number(ask_env, monkeypatch) -> None:
    from datetime import datetime as _dt

    calls = await _abstaining_env(
        ask_env,
        monkeypatch,
        {
            117968: {
                "is_pr": 1,
                "state": "open",
                "author": "dev-alice",
                "title": "Fix the thing",
                "opened_at": _dt(2026, 9, 10, 8, 0),
                "closed_at": None,
                "merged_at": None,
            },
        },
    )
    ask_env.llm = _FakeLLM(["should not be called"])
    out = await _run(query="What do we know about PR #117968?", gh_items_fallback=True)
    assert out.source == "record" and out.abstain is False
    assert (
        "#117968 is a PR" in out.answer
        and "opened by @dev-alice" in out.answer
        and "Fix the thing" in out.answer
    )
    assert out.memory_keys == ["gh_item:code-x:117968"]
    assert calls and calls[0][1] == "code-x" and 117968 in calls[0][2]
    assert ask_env.llm.calls == []  # no synthesis: the record IS the answer


async def test_gh_items_fallback_unknown_number_still_abstains(ask_env, monkeypatch) -> None:
    calls = await _abstaining_env(ask_env, monkeypatch, {})
    out = await _run(query="What do we know about PR #999999?", gh_items_fallback=True)
    assert (
        out.source == "abstained" and out.abstain is True
    )  # absence is unknown, never "no such PR"
    assert calls  # it did look


async def test_gh_items_fallback_on_by_default_off_per_request_needs_a_number(
    ask_env, monkeypatch
) -> None:
    """On by default; the per-request flag still switches it off."""
    from datetime import datetime as _dt

    calls = await _abstaining_env(
        ask_env,
        monkeypatch,
        {
            117968: {
                "is_pr": 1,
                "state": "open",
                "author": "a",
                "title": "t",
                "opened_at": _dt(2026, 9, 10),
            },
        },
    )
    out = await _run(query="What do we know about PR #117968?", gh_items_fallback=False)
    assert out.source == "abstained" and calls == []
    out = await _run(query="What do we know about the CI failures?", gh_items_fallback=True)
    assert out.source == "abstained" and calls == []
    out = await _run(query="What do we know about PR #117968?")
    assert out.source == "record" and calls


async def test_gh_items_fallback_replaces_the_hedged_synthesis(ask_env, monkeypatch) -> None:
    """Retrieval found neighbours about OTHER PRs (entity_absent → hedge path):
    the item record answers instead of a hedged synthesis."""
    from datetime import datetime as _dt

    import server.memory.app.search as hsearch
    from server.memory.api.models_search import MemorySearchOutput

    calls = await _abstaining_env(
        ask_env,
        monkeypatch,
        {
            119618: {
                "is_pr": 1,
                "state": "open",
                "author": "bob",
                "title": "Speed up merges",
                "opened_at": _dt(2026, 9, 11, 9, 0),
                "closed_at": None,
                "merged_at": None,
            },
        },
    )
    hits = [
        _hit("m1", "PR #117040 was merged after CI passed"),
        _hit("m2", "PR #116395 is waiting on review"),
    ]

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return MemorySearchOutput(
            results=hits,
            embedding_model="fake-embed",
            abstain=True,
            abstain_reason="identifier not cited",
            abstain_code="entity_absent",
        )

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    ask_env.llm = _FakeLLM(["hedged answer that should not be produced"])
    out = await _run(query="What do we know about PR #119618?", gh_items_fallback=True)
    assert (
        out.source == "record"
        and "#119618 is a PR" in out.answer
        and "Speed up merges" in out.answer
    )
    assert ask_env.llm.calls == [] and calls
    # Off per request (on is the default): the hedge path
    # synthesizes as before.
    ask_env.llm = _FakeLLM(["I have no memory specifically about #119618 [m1]."])
    out = await _run(query="What do we know about PR #119618?", gh_items_fallback=False)
    assert out.source == "synthesized" and len(ask_env.llm.calls) == 1


# ---- when-question fallback to dated raw turns -------------


@pytest.mark.asyncio
async def test_when_question_not_recorded_is_asked_again_over_dated_turns(
    ask_env, monkeypatch
) -> None:
    import server.memory.app.ask_when_evidence as awe

    calls = {"n": 0}

    async def fake_turns(**kw):
        calls["n"] += 1
        return [
            awe.DatedTurn(
                day="2023-03-13",
                role="user",
                content="Deborah: I started a yoga class last Friday!",
            )
        ]

    monkeypatch.setattr(awe, "fetch_dated_turns", fake_turns)
    ask_env.llm = _FakeLLM(
        [
            "The memories do not record when Deborah started yoga.",
            "Deborah started the yoga class on 2023-03-10, the Friday before the 2023-03-13 turn [m1].",
        ]
    )
    out = await _run(query="When did Deborah start the yoga class?", decompose="off")
    assert out.source == "synthesized" and out.when_evidence_turns == 1
    assert "2023-03-10" in out.answer
    assert len(ask_env.llm.calls) == 2
    second_system, second_user = ask_env.llm.calls[1][0], ask_env.llm.calls[1][1]
    assert "day each was SAID" in second_system and "[turn @ 2023-03-13]" in second_user
    # (decomposition off: its LLM call would otherwise consume the first fake reply)
    # a confident first answer is not re-asked
    calls["n"] = 0
    ask_env.llm = _FakeLLM(["Deborah started yoga on 2023-03-10 [m1]."])
    out = await _run(query="When did Deborah start the yoga class?", decompose="off")
    assert calls["n"] == 0 and out.when_evidence_turns == 0 and len(ask_env.llm.calls) == 1
    # A non-when question that says not recorded is
    # re-asked over the turns too, under the not-recorded note

    ask_env.llm = _FakeLLM(
        ["The memories do not record what Deborah watched.", "Deborah watched Dune."]
    )
    out = await _run(query="What movie did Deborah watch?", decompose="off")
    assert calls["n"] == 1 and out.when_evidence_turns == 1 and "Dune" in out.answer
    assert awe.NOT_RECORDED_NOTE.strip()[:40] in ask_env.llm.calls[1][0]


async def test_deep_first_answer_that_says_not_recorded_is_read_again_over_turns(monkeypatch):
    """The second reading also runs when the
    deep-first order served the deep answer and the first synthesis never ran."""
    from types import SimpleNamespace

    import server.memory.app.ask_when_evidence as awe
    from server.memory.app.ask import synthesis

    calls = {"n": 0, "kw": None}

    async def fake_turns(**_kw):
        calls["n"] += 1
        return [
            SimpleNamespace(text="I finished The Nightingale in three weeks", said_at="2023-05-24")
        ]

    async def fake_synth(_client, _q, _mem, _chars, **kw):
        calls["kw"] = kw
        return "Three weeks [turn @ 2023-05-24]"

    monkeypatch.setattr(awe, "fetch_dated_turns", fake_turns)
    monkeypatch.setattr(awe, "render_turns", lambda turns: "[turn @ 2023-05-24] " + turns[0].text)
    monkeypatch.setattr(synthesis, "_synthesize", fake_synth)
    second, n = await synthesis._not_recorded_second(
        None,
        "How long did The Nightingale take?",
        [],
        400,
        {"premise_system": "P."},
        search_input=SimpleNamespace(org="o", namespace="n", agent="a", user_id=None),
        answer="The memories do not record how long The Nightingale took.",
    )
    assert second.startswith("Three weeks") and n == 1
    assert (
        calls["kw"]["premise_system"].startswith("P.")
        and awe.NOT_RECORDED_NOTE.strip()[:30] in calls["kw"]["premise_system"]
    )
    assert "[turn @ 2023-05-24]" in calls["kw"]["raw_evidence"]
    # a confident answer is not read again
    second, n = await synthesis._not_recorded_second(
        None,
        "How long?",
        [],
        400,
        {},
        search_input=SimpleNamespace(),
        answer="Three weeks.",
    )
    assert second == "" and n == 0
