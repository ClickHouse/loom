"""memory.ask on the default path: no extra recall leg, no extra model call.

A characterization test for question shapes that could tempt an extra leg.
With the deployment defaults a sentiment-at-a-moment
question, a count question and an ordering question each answer from the
search window; a set answer that lists fewer members
than the question names is not read a second time; the answer carries no
in-call selection telemetry and no session restriction.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit


class _FakeLLM:
    provider = "fake"

    def __init__(self, reply: str) -> None:
        self.model = "fake"
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text=self.reply, model=self.model)


def _hit(key: str, excerpt: str, valid_at: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.9,
        rank_score=0.9,
        valid_at=valid_at,
        session_id="s1" if key != "m3" else "s2",
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.ask_when_evidence as awe
    import server.memory.app.search as hsearch
    import server.observability.budget as budget

    e = SimpleNamespace(searches=[], recalls=[])
    e.search_output = MemorySearchOutput(
        results=[
            _hit("m1", "user felt disappointed after the interview", "2024-10-02 07:40:00"),
            _hit("m2", "user took a road trip down the coast", "2024-09-20 10:00:00"),
            _hit("m3", "user went to Yosemite with friends", "2024-09-01 10:00:00"),
        ],
        embedding_model="fake",
    )
    e.llm = _FakeLLM("Two trips: the road trip [m2] and Yosemite [m3].")

    async def fake_search(inp: Any) -> MemorySearchOutput:
        e.searches.append(inp)
        return e.search_output

    async def over_cap(org: str, ns: str) -> bool:
        return False

    async def fake_turns(**kw: Any) -> list[Any]:
        e.recalls.append(kw)
        return []

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(awe, "fetch_dated_turns", fake_turns)
    monkeypatch.setattr(awe, "window_turns", fake_turns)
    return e


def _ask(q: str) -> MemoryAskInput:
    return MemoryAskInput(org="o", namespace="ns", agent="*", query=q)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "What is the most likely user sentiment around '2024-10-02 07:53' Wednesday?",
        "How many trips did I take in total?",
    ],
)
async def test_one_search_one_synthesis_window_is_the_search(env, question) -> None:
    from server.memory.app.ask.handler import handle_memory_ask

    out = await handle_memory_ask(_ask(question))
    assert len(env.searches) == 1
    assert not getattr(env.searches[0], "session_ids", None)
    assert env.recalls == []
    # The last call is the synthesis that answered (the deep pass's selector
    # may run before it); no call read a raw turn.
    system, user = env.llm.calls[-1]
    assert "raw conversation turns" not in system.lower()
    assert all("[turn @" not in u and "[user @" not in u for _, u in env.llm.calls)
    assert {h.memory_key for h in out.results} == {"m1", "m2", "m3"}
    assert out.gate_mode == "" and out.gate_kept == 0 and out.gate_dropped == 0
    assert "session_candidates" not in type(out).model_fields  # no session-candidate stage
    assert "Selected:" not in system


@pytest.mark.asyncio
async def test_short_set_answer_is_not_read_twice() -> None:
    from server.memory.app.ask import synthesis

    q = "What is the order of the three trips I took in the past three months, from earliest to latest?"
    answer = "From earliest to latest:\n1. **Road trip** [m2]\n2. **Yosemite** [m3]"
    second, n = await synthesis._not_recorded_second(
        None,
        q,
        [],
        400,
        {},
        search_input=SimpleNamespace(org="o", namespace="n", agent="a", user_id=None),
        answer=answer,
    )
    assert second == "" and n == 0
