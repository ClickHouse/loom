"""Typed (record_schema) extraction counts every token count of every call it makes."""

from __future__ import annotations

import pytest

from server.llm.client import LLMResult

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


@pytest.mark.asyncio
async def test_typed_extraction_counts_every_token_count_of_every_call(monkeypatch) -> None:
    from server.memory.app.extract import typed

    reply = '{"record": {"city": "Lisbon"}}'
    client = _Scripted(['{"present": true}', '{"fields": ["city"]}', reply])
    monkeypatch.setattr(typed, "get_llm_client", lambda: client)
    record, usage = await typed.extract_typed_record_iterative(
        _MESSAGES, {"city": "string"}, operator_policy=""
    )
    assert client.calls == 3 and record.record == {"city": "Lisbon"}
    assert (usage.tokens_in, usage.tokens_out) == (300, 30)
    assert (usage.cached_input_tokens, usage.cache_write_input_tokens) == (120, 15)
    assert usage.text == reply and usage.model == "scripted-model"
