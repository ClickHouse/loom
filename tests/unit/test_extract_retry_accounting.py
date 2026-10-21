"""Every call an extraction makes is counted, and the optional zero-fact
retry cannot fail an extraction whose first pass completed."""

from __future__ import annotations

import pytest

from server.llm.client import LLMResult
from server.memory.app.extract import extract_from_messages
from server.memory.app.extract.client import _merge_llm_results

_MESSAGES = [
    {
        "role": "user",
        "content": "We moved the team offsite to Lisbon this year. It runs for three days.",
    }
]


class _Scripted:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, system, user, max_tokens=1024, response_format=None):
        self.calls += 1
        return LLMResult(
            text=self.replies.pop(0),
            tokens_in=100,
            tokens_out=10,
            provider=self.provider,
            model=self.model,
            cached_input_tokens=40,
            cache_write_input_tokens=5,
        )


def _patch(monkeypatch: pytest.MonkeyPatch, client: _Scripted) -> None:
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)


@pytest.mark.asyncio
async def test_a_zero_fact_retry_that_finds_nothing_is_still_counted(monkeypatch) -> None:
    client = _Scripted(['{"facts": []}', '{"facts": []}'])
    _patch(monkeypatch, client)
    res = await extract_from_messages(
        _MESSAGES, judge=False, coverage_sweep=False, operator_policy=""
    )
    assert client.calls == 2 and res.facts == []
    assert (res.llm_result.tokens_in, res.llm_result.tokens_out) == (200, 20)
    assert res.llm_result.cached_input_tokens == 80
    assert res.llm_result.cache_write_input_tokens == 10
    assert res.llm_result.text == '{"facts": []}'


@pytest.mark.asyncio
async def test_an_unparseable_retry_leaves_the_first_pass_standing(monkeypatch) -> None:
    client = _Scripted(['{"facts": []}', "I'm sorry, I can't help with that."])
    _patch(monkeypatch, client)
    res = await extract_from_messages(
        _MESSAGES, judge=False, coverage_sweep=False, operator_policy=""
    )
    assert client.calls == 2 and res.facts == []
    assert res.llm_result.tokens_in == 200


def test_merge_carries_every_token_count() -> None:
    a = LLMResult("a", 1, 2, "p", "m", cached_input_tokens=3, cache_write_input_tokens=4)
    b = LLMResult("b", 10, 20, "p", "m", cached_input_tokens=30, cache_write_input_tokens=40)
    out = _merge_llm_results(a, b)
    assert (out.tokens_in, out.tokens_out) == (11, 22)
    assert (out.cached_input_tokens, out.cache_write_input_tokens) == (33, 44)
    assert out.text == "b" and _merge_llm_results(a, b, text="a\nb").text == "a\nb"


_FACT_REPLY = '{"facts": [{"key": "offsite_city", "value": "The team offsite is in Lisbon."}]}'


@pytest.mark.asyncio
async def test_the_judge_call_is_counted_and_the_extraction_keeps_its_text(monkeypatch) -> None:
    verdict = '{"verdicts": [{"key": "offsite_city", "verdict": "CORRECT"}]}'
    client = _Scripted([_FACT_REPLY, verdict])
    _patch(monkeypatch, client)
    res = await extract_from_messages(
        _MESSAGES, judge=True, coverage_sweep=False, operator_policy=""
    )
    assert client.calls == 2 and [f.judge_verdict for f in res.facts] == ["CORRECT"]
    assert (res.llm_result.tokens_in, res.llm_result.tokens_out) == (200, 20)
    assert (res.llm_result.cached_input_tokens, res.llm_result.cache_write_input_tokens) == (80, 10)
    assert res.llm_result.text == _FACT_REPLY


@pytest.mark.asyncio
async def test_an_unreadable_judge_reply_keeps_every_fact_counts_the_call_and_says_so(
    monkeypatch, caplog
) -> None:
    client = _Scripted([_FACT_REPLY, "no verdicts today"])
    _patch(monkeypatch, client)
    with caplog.at_level("WARNING"):
        res = await extract_from_messages(
            _MESSAGES, judge=True, coverage_sweep=False, operator_policy=""
        )
    assert [f.memory_key for f in res.facts] == ["offsite_city"] and res.dropped_judge_wrong == 0
    assert res.llm_result.tokens_in == 200
    assert any("judge reply unreadable" in r.message for r in caplog.records)
