"""Tests for structured/enumerated assistant-answer capture.

Flag `memory_capture_structured_answers` (default False): adds a prompt block +
Examples 8f/8g, and raises the content_excerpt ceiling for answer facts so the
full sequence/list survives verbatim and in order.
"""

from __future__ import annotations

import pytest

from server.llm.client import LLMResult
from server.memory.app.extract import (
    _STRUCTURED_ANSWER_PROMPT_BLOCK,
    EXTRACTION_PROMPT_VERSION,
    extract_from_messages,
)
from server.memory.domain.excerpt import EXCERPT_LIMIT, build_excerpt


# Ordered enumeration with the payload appended LAST, past the 1200-char ceiling.
def _long_parameter_answer() -> str:
    items = ", ".join(f"{i}. Synth control parameter number {i:02d}" for i in range(1, 60))
    return (
        "Assistant listed the synth parameters in order ("
        + items
        + ') and said the 27th parameter is "Sound".'
    )


_LONG_ANSWER = _long_parameter_answer()
_TOPIC = "the synth parameter list and the 27th parameter"


def _fake_client_returning(payload: str):
    class _FakeClient:
        provider = "fake-answer"
        model = "fake-answer-1"

        async def complete(
            self,
            system: str,
            user: str,
            max_tokens: int = 1024,
            response_format: dict[str, str] | None = None,
        ) -> LLMResult:
            self.last_system = system  # type: ignore[attr-defined]
            return LLMResult(
                text=payload,
                tokens_in=10,
                tokens_out=5,
                provider=self.provider,
                model=self.model,
            )

    return _FakeClient()


@pytest.mark.asyncio
async def test_prompt_block_absent_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_capture_structured_answers", False)
    client = _fake_client_returning('{"facts": []}')
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)
    await extract_from_messages(
        [{"role": "user", "content": "List the parameters in order please."}],
        judge=False,
        operator_policy="",
    )
    assert "STRUCTURED / ENUMERATED ASSISTANT ANSWERS" not in client.last_system
    assert "EXAMPLE 8f" not in client.last_system


@pytest.mark.asyncio
async def test_prompt_block_present_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_capture_structured_answers", True)
    client = _fake_client_returning('{"facts": []}')
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)
    # The full prompt carries the block; a one-line input alone would get the
    # compact tier, which never does.
    await extract_from_messages(
        [{"role": "user", "content": "List the parameters in order please."}],
        judge=False,
        compact_prompt=False,
        operator_policy="",
    )
    assert "STRUCTURED / ENUMERATED ASSISTANT ANSWERS" in client.last_system
    assert "EXAMPLE 8f" in client.last_system
    assert "EXAMPLE 8g" in client.last_system
    assert "PRECISION GUARD" in _STRUCTURED_ANSWER_PROMPT_BLOCK
    assert EXTRACTION_PROMPT_VERSION == "extraction-v1"


@pytest.mark.asyncio
async def test_structured_answer_yields_one_verbatim_ordered_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_capture_structured_answers", True)
    import json

    payload = json.dumps(
        {
            "facts": [
                {
                    "key": "assistant_listed_synth_parameters",
                    "value": _LONG_ANSWER,
                    "tags": ["answer", "reference"],
                    "entities": ["sound"],
                    "relations": [{"s": "assistant", "v": "listed", "o": "synth parameters"}],
                    "source_message_indices": [1],
                    "question_topic": _TOPIC,
                }
            ]
        }
    )
    monkeypatch.setattr(
        "server.memory.app.extract.client.get_llm_client",
        lambda: _fake_client_returning(payload),
    )
    result = await extract_from_messages(
        [
            {
                "role": "user",
                "content": "List the synth parameters in order; what is the 27th?",
            },
            {"role": "assistant", "content": _LONG_ANSWER},
            {"role": "user", "content": "got it, thanks"},
        ],
        judge=False,
        operator_policy="",
    )
    answer_facts = [f for f in result.facts if f.question_topic]
    assert len(answer_facts) == 1
    f = answer_facts[0]
    assert f.question_topic == _TOPIC
    assert ("assistant", "list", "synth parameters") in f.relations
    assert "1. Synth control parameter number 01" in f.value
    assert "27. Synth control parameter number 27" in f.value
    assert "59. Synth control parameter number 59" in f.value
    assert '27th parameter is "Sound"' in f.value
    assert (
        f.value.index("1. Synth control parameter number 01")
        < f.value.index("27. Synth control parameter number 27")
        < f.value.index("59. Synth control parameter number 59")
    )


def test_long_answer_excerpt_clipped_at_1200_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_capture_structured_answers", False)
    assert len(_LONG_ANSWER) > EXCERPT_LIMIT
    excerpt = build_excerpt(
        _LONG_ANSWER, memory_key="assistant_listed_synth_parameters", is_answer=True
    )
    assert len(excerpt) <= EXCERPT_LIMIT
    assert '27th parameter is "Sound"' not in excerpt


def test_long_answer_excerpt_preserved_whole_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_capture_structured_answers", True)
    excerpt = build_excerpt(
        _LONG_ANSWER, memory_key="assistant_listed_synth_parameters", is_answer=True
    )
    assert _LONG_ANSWER in excerpt
    assert "1. Synth control parameter number 01" in excerpt
    assert "27. Synth control parameter number 27" in excerpt
    assert "59. Synth control parameter number 59" in excerpt
    assert '27th parameter is "Sound"' in excerpt
    assert (
        excerpt.index("1. Synth control parameter number 01")
        < excerpt.index("27. Synth control parameter number 27")
        < excerpt.index("59. Synth control parameter number 59")
    )


def test_raised_ceiling_only_applies_to_answer_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_capture_structured_answers", True)
    long_user_fact = "User said: " + ("blah " * 400)  # > 1200 chars
    assert len(long_user_fact) > EXCERPT_LIMIT
    excerpt = build_excerpt(long_user_fact, memory_key="user_note", is_answer=False)
    assert len(excerpt) <= EXCERPT_LIMIT


@pytest.mark.asyncio
async def test_noncommittal_option_listing_produces_no_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_capture_structured_answers", True)
    monkeypatch.setattr(
        "server.memory.app.extract.client.get_llm_client",
        lambda: _fake_client_returning('{"facts": []}'),
    )
    result = await extract_from_messages(
        [
            {"role": "user", "content": "What font should I use for the deck?"},
            {
                "role": "assistant",
                "content": (
                    "There are lots of good choices — you might consider "
                    "Helvetica, Georgia, or Futura, among many others. It "
                    "really depends on the vibe; up to you!"
                ),
            },
            {"role": "user", "content": "ok I'll think about it"},
        ],
        judge=False,
        operator_policy="",
    )
    assert result.facts == []


@pytest.mark.asyncio
async def test_chatter_does_not_leak_as_topic_keyed_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_capture_structured_answers", True)
    payload = (
        '{"facts": [{"key": "assistant_suggested_chord_progression", '
        '"value": "Assistant suggested the C-major chord progression '
        'C - D - E - F - G - A - B - A - G - F - E - D - C.", '
        '"tags": ["answer", "music"], '
        '"question_topic": "the chord progression you suggested"}]}'
    )
    monkeypatch.setattr(
        "server.memory.app.extract.client.get_llm_client",
        lambda: _fake_client_returning(payload),
    )
    result = await extract_from_messages(
        [
            {"role": "user", "content": "A simple major-scale chord progression?"},
            {
                "role": "assistant",
                "content": (
                    "Great question, happy to help! Try this in C major: "
                    "C - D - E - F - G - A - B - A - G - F - E - D - C."
                ),
            },
            {"role": "user", "content": "thanks, I'll try that on the weekend"},
        ],
        judge=False,
        operator_policy="",
    )
    answer_facts = [f for f in result.facts if f.question_topic]
    assert len(answer_facts) == 1
    assert "C - D - E - F - G - A - B - A - G - F - E - D - C" in answer_facts[0].value
    for f in result.facts:
        assert "offered to help" not in f.value.lower()
        assert "great question" not in f.value.lower()
