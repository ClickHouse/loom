"""memory.ask question decomposition: cue gate, reply parsing,
and the per-part retrieval union, with the search handler stubbed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from server.memory.app.ask import question_shapes, retrieval_legs


def test_multihop_cues() -> None:
    assert question_shapes.looks_multihop(
        "How long after Caroline adopted her dog did she move to Boston?"
    )
    assert question_shapes.looks_multihop(
        "Which of Melanie's hobbies did she pick up before her trip?"
    )
    assert question_shapes.looks_multihop("Did Jon travel more than Gina last year?")
    assert not question_shapes.looks_multihop("What is Caroline's dog called?")
    assert not question_shapes.looks_multihop("")


def test_date_of_event_cues_fire_the_gate() -> None:
    """'When did X do Y?' lookups are the questions decomposition helps most;
    the gate fires on them."""
    assert question_shapes.looks_multihop("When did Caroline go to the LGBTQ conference?")
    assert question_shapes.looks_multihop("When was Melanie's friend's child adopted?")
    assert question_shapes.looks_multihop(
        "What year did Gina get accepted for the design internship?"
    )
    assert question_shapes.looks_multihop("In which month did Jon open his studio?")
    assert question_shapes.looks_multihop("How long ago did Caroline pass the adoption interview?")
    # a 'when' clause inside a plain lookup is not the question's shape
    assert not question_shapes.looks_multihop("Where did Caroline move from 4 years ago?")
    assert not question_shapes.looks_multihop("Does Melanie know when the exhibition opens?")
    assert not question_shapes.looks_multihop("What is Caroline's relationship status?")


def test_parse_decomposition_cleans_and_caps() -> None:
    q = "How long after Caroline adopted her dog did she move to Boston?"
    text = (
        "1. When did Caroline adopt her dog\n- When did Caroline move to Boston?\n\n"
        "When did Caroline move to Boston?\nHow long after Caroline adopted her dog did she move to Boston?"
    )
    parts = retrieval_legs.parse_decomposition(text, q)
    assert parts == ["When did Caroline adopt her dog?", "When did Caroline move to Boston?"]
    assert retrieval_legs.parse_decomposition("NONE", q) == []
    # a single part is not a decomposition
    assert retrieval_legs.parse_decomposition("Only one line?", q) == []
    many = "\n".join(f"Sub question {i}?" for i in range(8))
    assert len(retrieval_legs.parse_decomposition(many, q, max_parts=3)) == 3


@dataclass
class _Hit:
    memory_key: str
    similarity: float = 0.5


class _Res:
    def __init__(self, keys: list[str]) -> None:
        self.results = [_Hit(k) for k in keys]


class _LLM:
    model = "fake-cheap"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, system: str, user: str, max_tokens: int = 0) -> Any:
        self.calls += 1

        class _R:
            text = self.text
            model = "fake-cheap"

        return _R()


@dataclass
class _SearchInput:
    org: str = "o"
    namespace: str = "n"
    agent: str = "*"
    query: str = "q"
    top_k: int = 10


@pytest.mark.asyncio
async def test_decomposed_retrieval_unions_per_part_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int]] = []

    async def _search(inp: Any) -> _Res:
        seen.append((inp.query, inp.top_k))
        return _Res(
            {
                "When did Caroline adopt her dog?": ["adopt_2021", "shared"],
                "When did Caroline move to Boston?": ["move_2023", "shared"],
            }[inp.query]
        )

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    llm = _LLM("When did Caroline adopt her dog?\nWhen did Caroline move to Boston?")
    q = "How long after Caroline adopted her dog did she move to Boston?"
    parts, merged = await retrieval_legs.decomposed_retrieval(
        _SearchInput(query=q),
        q,
        [_Hit("shared"), _Hit("whole_only")],
        llm=llm,
        mode="always",
        part_top_k=6,
    )
    assert parts == ["When did Caroline adopt her dog?", "When did Caroline move to Boston?"]
    assert [h.memory_key for h in merged] == ["shared", "whole_only", "adopt_2021", "move_2023"]
    assert seen == [
        ("When did Caroline adopt her dog?", 6),
        ("When did Caroline move to Boston?", 6),
    ]
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_auto_mode_skips_single_hop_without_an_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _LLM("x\ny")
    parts, merged = await retrieval_legs.decomposed_retrieval(
        _SearchInput(),
        "What is Caroline's dog called?",
        [_Hit("a")],
        llm=llm,
        mode="auto",
    )
    assert parts == [] and [h.memory_key for h in merged] == ["a"] and llm.calls == 0


@pytest.mark.asyncio
async def test_none_reply_and_failures_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _LLM("NONE")
    parts, merged = await retrieval_legs.decomposed_retrieval(
        _SearchInput(),
        "Did Jon travel more than Gina?",
        [_Hit("a")],
        llm=llm,
        mode="always",
    )
    assert parts == [] and len(merged) == 1

    class _Boom:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("provider down")

    parts, merged = await retrieval_legs.decomposed_retrieval(
        _SearchInput(),
        "Did Jon travel more than Gina?",
        [_Hit("a")],
        llm=_Boom(),
        mode="always",
    )
    assert parts == [] and len(merged) == 1

    async def _search_fail(inp: Any) -> _Res:
        raise RuntimeError("ch down")

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search_fail)
    stats: dict[str, int] = {"failed": 0}
    parts, merged = await retrieval_legs.decomposed_retrieval(
        _SearchInput(),
        "Did Jon travel more than Gina?",
        [_Hit("a")],
        llm=_LLM("How much did Jon travel?\nHow much did Gina travel?"),
        mode="always",
        stats=stats,
    )
    assert len(parts) == 2 and len(merged) == 1  # parts recorded, nothing added
    # The fail-open swallow is counted, not only logged.
    assert stats["failed"] == 2


def test_input_field_defaults_off() -> None:
    from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput

    assert MemoryAskInput(org="o", namespace="n", query="q").decompose == ""
    out = MemoryAskOutput(answer="", source="abstained")
    assert out.decomposed == [] and out.decomposed_parts == 0


def test_decompose_failed_and_pool_k_ride_the_ledger() -> None:
    """An inert lever is a number on the row, not a log line."""
    from server.memory.api.models_ask import MemoryAskOutput
    from server.observability.event_args import project_result

    out = MemoryAskOutput(answer="", source="abstained", decompose_failed=2, pool_k=30)
    assert out.decompose_failed == 2 and out.pool_k == 30
    projected = project_result(out)
    assert projected["out_decompose_failed"] == "2"
    assert projected["out_pool_k"] == "30"
    assert MemoryAskOutput(answer="", source="abstained").decompose_failed == 0


@pytest.mark.asyncio
async def test_a_part_adds_another_sessions_row_of_a_pooled_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a key holds a row per session: a part that finds the other session's row adds it
    from types import SimpleNamespace

    def row(session: str) -> Any:
        hit = SimpleNamespace(memory_key="k", session_id=session, similarity=0.5)
        hit._row_identity = ("a", "u", session)
        return hit

    async def _search(inp: Any) -> Any:
        return SimpleNamespace(results=[row("s1"), row("s2")])

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    q = "How long after Caroline adopted her dog did she move to Boston?"
    _, merged = await retrieval_legs.decomposed_retrieval(
        _SearchInput(query=q), q, [row("s1")], llm=_LLM("part one\npart two"), mode="always"
    )
    assert [h.session_id for h in merged] == ["s1", "s2"]


def test_the_pool_takes_a_new_row_but_not_a_record_of_a_pooled_key() -> None:
    from types import SimpleNamespace

    def hit(key: str, identity: Any = None) -> Any:
        h = SimpleNamespace(memory_key=key, session_id="")
        h._row_identity = identity
        return h

    pooled = retrieval_legs.PooledRows([hit("k", ("a", "u", "s1"))], "n")
    assert pooled.add(hit("k", ("a", "u", "s2")))  # another session's row of the key
    assert not pooled.add(hit("k", ("a", "u", "s1")))  # the same row again
    assert not pooled.add(hit("k"))  # a record of a pooled key names no row
    assert pooled.add(hit("j"))  # a key the pool does not hold
    assert not pooled.add(hit("j"))
