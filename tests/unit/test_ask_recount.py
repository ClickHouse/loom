"""memory.ask recount: the ANSWER number follows the model's
own ITEMS verdicts when the two disagree."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit
from server.memory.domain.ask_recount import recount

_FIVE_LISTED_FOUR_ANSWERED = """ITEMS:
- grandmother's clock — qualifies [m1]
- great-uncle's compass — qualifies [m2]
- mother's ring — qualifies [m3]
- father's watch — qualifies [m4]
- aunt's vase — qualifies [m5]
- neighbour's lamp — fails: bought, not inherited [m6]
ANSWER: 4 antique items inherited or acquired from family [m1] [m2] [m3] [m4]"""


def test_recount_rewrites_a_miscounted_answer_and_keeps_the_rest() -> None:
    rc = recount(_FIVE_LISTED_FOUR_ANSWERED)
    assert rc.recomputed and rc.qualifies == 5 and rc.stated == 4
    ans = [ln for ln in rc.text.splitlines() if ln.startswith("ANSWER:")][0]
    assert ans.startswith("ANSWER: 5 antique items inherited or acquired from family [m1]")
    assert ans.endswith("(recounted from the listed items)")
    # ITEMS untouched.
    assert rc.text.count("— qualifies") == 5


def test_recount_leaves_a_consistent_answer_alone() -> None:
    text = _FIVE_LISTED_FOUR_ANSWERED.replace("ANSWER: 4", "ANSWER: 5")
    rc = recount(text)
    assert not rc.recomputed and rc.text == text and rc.stated == 5


def test_recount_handles_unclear_lines_with_the_paren_form() -> None:
    text = """ITEMS:
- a — qualifies
- b — qualifies
- c — unclear: approximate
ANSWER: 12 (13 if the unclear ones count) [k]"""
    rc = recount(text)
    assert rc.recomputed and rc.qualifies == 2 and rc.unclear == 1
    assert (
        "ANSWER: 2 (3 if the unclear ones count) [k] (recounted from the listed items)" in rc.text
    )


def test_recount_accepts_number_words_and_skips_two_part_answers() -> None:
    text = "ITEMS:\n- a — qualifies\n- b — qualifies\nANSWER: twelve siblings [k]"
    rc = recount(text)
    assert rc.recomputed and rc.text.splitlines()[-1].startswith("ANSWER: 2 siblings [k]")
    two_part = "ITEMS:\n- a — qualifies\nANSWER: 3 events; 5 people [k]"
    assert not recount(two_part).recomputed


def test_recount_leaves_a_list_of_stated_quantities_alone() -> None:
    text = (
        "ITEMS:\n- tomato plants initially planted — stated: 5 [k1]\n"
        "- cucumber plants initially planted — stated: 3 [k2]\nANSWER: 8 plants [k1] [k2]"
    )
    rc = recount(text)
    assert not rc.recomputed and rc.text == text
    mixed = "ITEMS:\n- shirts packed: 7 — stated: 7 [k]\n- a trip — qualifies\nANSWER: 7 shirts [k]"
    assert not recount(mixed).recomputed


def test_recount_ignores_unknown_block_and_unverdicted_lists() -> None:
    text = "ITEMS:\n- a — qualifies\nUNKNOWN:\n- z — qualifies?\nANSWER: 2 [k]"
    rc = recount(text)
    assert rc.recomputed and rc.qualifies == 1
    plain = "ITEMS:\n- a\n- b\nANSWER: 5 [k]"  # no verdict markers: not trusted
    assert not recount(plain).recomputed
    assert not recount("just prose").recomputed
    assert not recount("").recomputed


# ---- through the handler -------------------------------------------------------


class _FakeLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text=self._text)


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
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace(llm=_FakeLLM(_FIVE_LISTED_FOUR_ANSWERED), settings=settings)
    e.search_output = MemorySearchOutput(
        results=[_hit(f"m{i}", f"antique item {i} from family") for i in range(1, 7)],
        embedding_model="fake-embed",
    )

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return e.search_output

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    return e


async def _run(**overrides: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "How many antique items did I inherit or acquire from my family members?",
    }
    base.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**base))


async def test_count_question_is_recounted_through_the_handler(env) -> None:
    out = await _run()
    assert out.count_recomputed is True
    # The fold puts the ANSWER first; it now carries the recounted 5.
    assert out.answer.startswith("5 antique items inherited or acquired from family")
    assert "(recounted from the listed items)" in out.answer.splitlines()[0]


async def test_non_count_question_never_recounts(env) -> None:
    env.llm._text = "Blue [m1]."
    out = await _run(query="what is my favorite colour")
    assert out.count_recomputed is False and out.answer == "Blue [m1]."


async def test_per_call_false_keeps_the_model_number(env) -> None:
    out = await _run(recount=False)
    assert out.count_recomputed is False
    assert out.answer.startswith("4 antique items")


async def test_count_prompt_carries_the_stated_quantity_rule(env) -> None:
    await _run()
    # The first SYNTHESIS call (the deep selector's pick call can precede it).
    system = next(s for s, _u in env.llm.calls if "worth opening at full detail" not in s)
    assert "— stated: N" in system and "never the number of lines" in system
    assert "does not make an item unclear" in system
    assert "do not answer 0" in system  # an empty list is an absence, not a zero
