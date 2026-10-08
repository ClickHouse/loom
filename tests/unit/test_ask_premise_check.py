"""memory.ask checks the question's premise against pinned / verified memories:
parsing, trusted-row folding, the correction note, and the
ask() flow — a challenge that contradicts a pinned memory is corrected, not
adopted; no trusted rows means no judge call; off leaves the wire empty."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit
from server.memory.app import answer_consistency as ac


def test_parse_premise_requires_a_named_memory_for_a_conflict() -> None:
    v = ac.parse_premise(
        '{"conflict": true, "memory_key": "k1", "premise": "PR merged", "correction": "PR is open"}'
    )
    assert v.ran and v.conflict and v.memory_key == "k1" and v.correction == "PR is open"
    v2 = ac.parse_premise('{"conflict": true}')
    assert v2.ran and not v2.conflict
    v3 = ac.parse_premise('{"conflict": false, "memory_key": "k1", "premise": "x"}')
    assert v3.ran and not v3.conflict and v3.memory_key == "" and v3.premise == ""
    assert not ac.parse_premise("prose").ran and not ac.parse_premise('{"x": 1}').ran


def test_trusted_rows_fold_to_pool_order_and_labels() -> None:
    rows = [
        {"memory_key": "b", "pinned": 1, "verified_by": "", "attested": 0},
        {"memory_key": "a", "pinned": 0, "verified_by": "human:avery", "attested": 1},
        {"memory_key": "c", "pinned": 0, "verified_by": "", "attested": 0},
        {"memory_key": "zz", "pinned": 1, "verified_by": "human:x", "attested": 1},
        # attested once, content changed since: not trusted, not labelled verified
        {"memory_key": "d", "pinned": 0, "verified_by": "human:avery", "attested": 0},
        {"memory_key": "e", "pinned": 1, "verified_by": "human:avery", "attested": 0},
    ]
    out = ac.trusted_rows(rows, ["a", "b", "c", "d", "e"])
    assert [t.memory_key for t in out] == ["a", "b", "e"]
    assert out[0].label == "human-verified" and out[1].label == "pinned"
    assert out[2].label == "pinned"
    assert ac.TrustedRow("q", True, "human:x").label == "pinned, human-verified"


def test_premise_note_holds_the_trusted_fact() -> None:
    rows = [ac.TrustedRow("k1", pinned=True)]
    v = ac.PremiseVerdict(
        conflict=True, memory_key="k1", premise="PR 1 merged", correction="PR 1 is open", ran=True
    )
    note = ac.premise_note(v, rows)
    assert (
        "[k1] (pinned)" in note
        and "It presupposes: PR 1 merged." in note
        and "Do NOT adopt" in note
    )
    assert ac.premise_note(ac.PremiseVerdict(), rows) == ""


@pytest.mark.asyncio
async def test_premise_check_only_names_shown_memories_and_fails_open() -> None:
    class Says:
        def __init__(self, t: str) -> None:
            self.t = t

        async def complete(self, *a: Any, **k: Any) -> Any:
            return SimpleNamespace(text=self.t)

    rows = [ac.TrustedRow("k1", pinned=True)]
    mem = [SimpleNamespace(memory_key="k1", content_excerpt="PR 1 is open")]
    v = await ac.premise_check(
        Says('{"conflict": true, "memory_key": "other"}'),
        "since PR 1 merged, what next?",
        mem,
        rows,
    )
    assert v.ran and not v.conflict

    class Boom:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    assert not (await ac.premise_check(Boom(), "q", mem, rows)).ran
    assert not (await ac.premise_check(Says("{}"), "q", [], rows)).ran


# ---------------------------------------------------------------- ask() flow


class _FakeLLM:
    provider = "fake"

    def __init__(self, replies: list[str]) -> None:
        self.model = "fake"
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        text = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        return SimpleNamespace(text=text, model=self.model)


def _hit(key: str, excerpt: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.9,
        rank_score=0.9,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # The temporal raw pass is on by default; these tests script the LLM
    # replies for other mechanisms, so pin it off here.

    monkeypatch.setattr("server.memory.app.ask.pool.looks_temporal", lambda *a, **k: False)
    import server.core.ch as ch_mod
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace()
    e.trusted_rows: list[dict[str, Any]] = [{"memory_key": "m_pin", "pinned": 1, "verified_by": ""}]
    e.search_output = MemorySearchOutput(
        results=[
            _hit("m_pin", "PR 117959 is still open, waiting on CI."),
            _hit("m2", "CI is flaky this week."),
        ],
        embedding_model="fake",
    )
    e.llm = _FakeLLM(
        [
            '{"conflict": true, "memory_key": "m_pin", "premise": "PR 117959 was merged", "correction": "PR 117959 is open, waiting on CI"}',
            "PR 117959 has not merged: it is open and waiting on CI [m_pin]; the premise is contradicted by that pinned memory.",
        ]
    )

    class _CH:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            from server.memory.store.sql import attested_sql

            assert f"pinned = 1 OR {attested_sql()}" in sql and "AS attested" in sql
            return [{**r, "attested": int(bool(r.get("verified_by")))} for r in e.trusted_rows]

    async def fake_search(inp: Any) -> MemorySearchOutput:
        return e.search_output

    async def over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    for k, v in {
        "memory_ask_synth_model": "",
        "memory_ask_synth_model_cheap": "",
        "memory_ask_premise_check": True,
        "memory_ask_consistency_check": False,
        "memory_ask_recency_weight": 0.0,
        "memory_ask_code_enrichment": False,
        "memory_ask_code_staleness": False,
        "memory_ask_identifier_injection": False,
    }.items():
        monkeypatch.setattr(settings, k, v)
    return e


def _ask() -> MemoryAskInput:
    return MemoryAskInput(
        org="o",
        namespace="ns",
        agent="*",
        query="since PR 117959 merged yesterday, what should I do next?",
    )


@pytest.mark.asyncio
async def test_contradicted_premise_is_corrected_not_adopted(env) -> None:
    from server.memory.app.ask.handler import handle_memory_ask

    out = await handle_memory_ask(_ask())
    assert out.premise_conflict == "m_pin"
    assert (
        out.premise_note
        == "question presupposes: PR 117959 was merged; [m_pin] records: PR 117959 is open, waiting on CI"
    )
    # judge call, then synthesis whose system prompt carries the correction.
    assert len(env.llm.calls) == 2
    judge_sys, judge_user = env.llm.calls[0]
    assert (
        "TRUSTED memory" in judge_sys
        and "[m_pin] (pinned)" in judge_user
        and "[m2]" not in judge_user
    )
    synth_sys, _ = env.llm.calls[1]
    assert "[m_pin] (pinned) contradicts" in synth_sys and "Do NOT adopt" in synth_sys


@pytest.mark.asyncio
async def test_no_trusted_rows_means_no_judge_call(env) -> None:
    from server.memory.app.ask.handler import handle_memory_ask

    env.trusted_rows = []
    env.llm._replies = ["PR 117959 is open [m_pin]."]
    out = await handle_memory_ask(_ask())
    assert out.premise_conflict == "" and out.premise_note == ""
    assert len(env.llm.calls) == 1


@pytest.mark.asyncio
async def test_premise_check_off_leaves_wire_empty(env, monkeypatch) -> None:
    from server.memory.app.ask.handler import handle_memory_ask
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_premise_check", False)
    env.llm._replies = ["PR 117959 is open [m_pin]."]
    out = await handle_memory_ask(_ask())
    assert out.premise_conflict == "" and len(env.llm.calls) == 1


@pytest.mark.asyncio
async def test_premise_check_judges_trusted_memories_one_at_a_time() -> None:
    """One trusted memory per prompt finds the presupposition far more often
    (12/16) than three at once (3/16) or eleven (1/16). The judge sees one, in
    pool order, at most three, and the first conflict wins."""
    seen: list[list[str]] = []

    class Judge:
        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            keys = [line.split("]")[0][1:] for line in user.splitlines() if line.startswith("[")]
            seen.append(keys)
            if keys == ["k2"]:
                return SimpleNamespace(
                    text='{"conflict": true, "memory_key": "k2", "premise": "p", "correction": "c"}'
                )
            return SimpleNamespace(text='{"conflict": false}')

    rows = [ac.TrustedRow(k, pinned=True) for k in ("k1", "k2", "k3", "k4")]
    mem = [
        SimpleNamespace(memory_key=k, content_excerpt=f"fact {k}") for k in ("k1", "k2", "k3", "k4")
    ]
    v = await ac.premise_check(Judge(), "since not-c, what next?", mem, rows)
    assert v.conflict and v.memory_key == "k2"
    assert seen == [["k1"], ["k2"]]  # one memory per prompt, stopped at the first conflict

    seen.clear()
    rows_no = [ac.TrustedRow(k, pinned=True) for k in ("k1", "k3", "k4", "k5")]
    mem_no = [SimpleNamespace(memory_key=k, content_excerpt="f") for k in ("k1", "k3", "k4", "k5")]
    v = await ac.premise_check(Judge(), "q", mem_no, rows_no)
    assert not v.conflict and v.ran
    assert seen == [["k1"], ["k3"], ["k4"]]  # capped at three
