"""Truncation-shaped extraction parse failures must not 502 the ingest.

Re-calling the LLM with the SAME max_tokens on a parse failure makes
truncation deterministic: the retry truncates at the same byte and the whole
pair is lost. The extractor detects the truncation shape, retries once with a
raised budget, and — if the larger
attempt still truncates — salvages the complete fact objects from the
prefix instead of failing the write.
"""

from __future__ import annotations

import pytest

from server.llm.client import LLMResult
from server.memory.app.extract import ExtractionError, extract_from_messages
from server.memory.app.extract.parse import _looks_truncated, _salvage_fact_objects

_TRUNCATED = (
    '{"facts": ['
    '{"key": "k1", "value": "first complete fact", "tags": ["a"]},'
    '{"key": "k2", "value": "second complete fact"},'
    '{"key": "k3", "value": "cut off mid-str'
)
_COMPLETE = (
    '{"facts": ['
    '{"key": "k1", "value": "first complete fact", "tags": ["a"]},'
    '{"key": "k2", "value": "second complete fact"},'
    '{"key": "k3", "value": "third complete fact"}'
    "]}"
)


def test_looks_truncated_shapes() -> None:
    assert _looks_truncated(_TRUNCATED)
    assert not _looks_truncated(_COMPLETE)
    assert not _looks_truncated("no json here at all")
    assert not _looks_truncated("")


def test_salvage_recovers_complete_prefix_objects() -> None:
    objs = _salvage_fact_objects(_TRUNCATED)
    assert objs is not None
    assert [o["key"] for o in objs] == ["k1", "k2"]


def test_salvage_returns_none_when_nothing_complete() -> None:
    assert _salvage_fact_objects('{"facts": [{"key": "k1", "va') is None
    assert _salvage_fact_objects("prose, no facts array") is None


class _SequenceClient:
    """LLM stub yielding scripted responses; records max_tokens per call."""

    provider = "scripted"
    model = "scripted-model"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[int] = []

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        self.calls.append(max_tokens)
        text = self._texts.pop(0) if self._texts else "{}"
        return LLMResult(
            text=text,
            tokens_in=10,
            tokens_out=5,
            provider=self.provider,
            model=self.model,
        )


_MSGS = [
    {
        "role": "user",
        "content": "A long substantive conversation. With several sentences. "
        "Enough content that zero facts would look like a misfire.",
    }
]


@pytest.mark.asyncio
async def test_truncation_retries_with_raised_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequenceClient([_TRUNCATED, _COMPLETE])
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)

    result = await extract_from_messages(_MSGS, max_tokens=1024, judge=False, operator_policy="")

    assert client.calls == [1024, 2048], "retry must double the budget"
    assert [f.memory_key for f in result.facts] == ["k1", "k2", "k3"]
    assert result.truncation == "retried", "the budget miss is reported, not hidden"
    # Token spend accumulates across both attempts.
    assert result.llm_result.tokens_in == 20
    assert result.llm_result.tokens_out == 10


@pytest.mark.asyncio
async def test_truncation_retry_budget_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequenceClient([_TRUNCATED, _COMPLETE])
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)

    await extract_from_messages(_MSGS, max_tokens=6000, judge=False, operator_policy="")

    assert client.calls == [6000, 8192]


@pytest.mark.asyncio
async def test_still_truncated_salvages_prefix_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequenceClient([_TRUNCATED, _TRUNCATED])
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)

    result = await extract_from_messages(_MSGS, max_tokens=1024, judge=False, operator_policy="")

    # The complete prefix objects land; the cut-off trailing one is dropped.
    assert [f.memory_key for f in result.facts] == ["k1", "k2"]
    assert len(client.calls) == 2
    assert result.truncation == "salvaged", "a lost tail is reported as such"


@pytest.mark.asyncio
async def test_unsalvageable_truncation_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsalvageable = '{"facts": [{"key": "k1", "va'
    client = _SequenceClient([unsalvageable, unsalvageable])
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)

    with pytest.raises(ExtractionError):
        await extract_from_messages(_MSGS, max_tokens=1024, judge=False, operator_policy="")


@pytest.mark.asyncio
async def test_non_truncation_parse_failure_keeps_strict_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage that is NOT truncation-shaped keeps the original posture:
    one same-budget retry with the strict addendum."""
    client = _SequenceClient(["pure prose, not JSON", _COMPLETE])
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)

    result = await extract_from_messages(_MSGS, max_tokens=1024, judge=False, operator_policy="")

    assert client.calls == [1024, 1024]
    assert len(result.facts) == 3
    assert result.truncation == "", "a non-truncation parse failure is not a budget miss"
