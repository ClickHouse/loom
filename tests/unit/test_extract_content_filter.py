"""A reply the provider's content filter cut (a quoted passage recited word for
word) is asked again without the recitation; a bigger budget would be cut at
the same place and the write would fail."""

from __future__ import annotations

import asyncio
from typing import Any

from server.llm.client import LLMResult
from server.memory.app.extract import extract as ex
from server.memory.app.extract.parse import _parse_fact_list


class _Client:
    def __init__(self, replies: list[LLMResult]) -> None:
        self.replies = list(replies)
        self.systems: list[str] = []

    async def complete(self, *, system: str, user: str, **_: Any) -> LLMResult:
        self.systems.append(system)
        return self.replies.pop(0)


def _r(text: str, stop: str = "stop") -> LLMResult:
    return LLMResult(
        text=text, tokens_in=10, tokens_out=5, provider="t", model="m", stop_reason=stop
    )


def test_a_filtered_reply_is_asked_again_without_the_recitation() -> None:
    cut = _r(
        '{"facts":[{"key":"plato_quote","value":"Plato wrote \\"This invention', "content_filter"
    )
    ok = _r('{"facts":[{"key":"plato_passage","value":"The passage is from Plato\'s Phaedrus."}]}')
    client = _Client([cut, ok])

    result, facts, _ = asyncio.run(
        ex._complete_and_parse(
            client,
            base_prompt="SYS",
            operator_policy="",
            user_prompt="U",
            max_tokens=100,
            json_format={"type": "json_object"},
            parse=_parse_fact_list,
        )
    )

    assert [f.memory_key for f in facts] == ["plato_passage"]
    assert client.systems[1].startswith("SYS") and "content filter" in client.systems[1]
    assert result.tokens_in == 20  # both calls are counted


def test_a_filtered_reply_on_the_truncation_retry_is_asked_again_too() -> None:
    # A cached cut reply carries no stop reason: it reads as truncated, the
    # bigger-budget retry is cut by the filter again, and that retry is re-asked.
    cached_cut = _r('{"facts":[{"key":"plato_quote","value":"Plato wrote \\"This invention')
    cut_again = _r('{"facts":[{"key":"plato_quote","value":"Plato wrote', "content_filter")
    ok = _r('{"facts":[{"key":"plato_passage","value":"The passage is from Plato\'s Phaedrus."}]}')
    client = _Client([cached_cut, cut_again, ok])

    _, facts, _ = asyncio.run(
        ex._complete_and_parse(
            client,
            base_prompt="SYS",
            operator_policy="",
            user_prompt="U",
            max_tokens=100,
            json_format={"type": "json_object"},
            parse=_parse_fact_list,
        )
    )

    assert [f.memory_key for f in facts] == ["plato_passage"]
    assert len(client.systems) == 3 and "content filter" in client.systems[2]


def test_an_unparseable_cached_reply_is_asked_of_the_provider_again() -> None:
    from dataclasses import replace

    from server.llm import cache as lc

    cached = replace(_r('{"facts":[{"key":"k","value":"cut'), from_cache=True)
    ok = _r('{"facts":[{"key":"k","value":"Whole."}]}')
    seen_bypass: list[bool] = []

    class _C(_Client):
        async def complete(self, *, system: str, user: str, **kw: Any) -> LLMResult:
            seen_bypass.append(lc._BYPASS.get())
            return await super().complete(system=system, user=user, **kw)

    client = _C([cached, ok])
    _, facts, _ = asyncio.run(
        ex._complete_and_parse(
            client,
            base_prompt="SYS",
            operator_policy="",
            user_prompt="U",
            max_tokens=100,
            json_format={"type": "json_object"},
            parse=_parse_fact_list,
        )
    )
    assert [f.value for f in facts] == ["Whole."]
    assert seen_bypass == [False, True]
