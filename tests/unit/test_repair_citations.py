"""A fact cites the turn it came from."""

from __future__ import annotations

import json

import pytest

from server.memory.app.extract import ExtractedFact
from server.memory.domain.extraction_citations import repair_citations

MESSAGES = [
    {
        "role": "user",
        "content": "I noticed a slight misfire on my commute a while back, but after replacing "
        "the spark plugs with new ones from NGK the engine felt much smoother.",
    },
    {
        "role": "assistant",
        "content": "Glad the plugs helped. For the misfire, checking the ignition coils and the "
        "fuel injectors would be the next diagnostic steps.",
    },
    {
        "role": "user",
        "content": "I'm also considering upgrading my brake pads, I got a discount on a set at "
        "the Winter Tune-Up event at the Speed Shop earlier this year.",
    },
]


def test_a_fact_cited_to_the_wrong_turn_is_repointed() -> None:
    f = ExtractedFact(
        memory_key="user_spark_plug_replacement",
        value="User replaced the spark plugs with NGK ones after a misfire on their commute, "
        "and the engine felt smoother.",
        source_message_indices=[2],
    )
    (out,) = repair_citations([f], MESSAGES)
    assert out.source_message_indices == [0]


def test_an_assistant_turn_is_never_the_repair_target() -> None:
    f = ExtractedFact(
        memory_key="k",
        value="Assistant suggested checking the ignition coils and fuel injectors as the next "
        "diagnostic steps.",
        source_message_indices=[2],
    )
    (out,) = repair_citations([f], MESSAGES)
    assert out.source_message_indices == [2]  # no user turn clears the bar


def test_a_correct_citation_is_left_alone() -> None:
    f = ExtractedFact(
        memory_key="user_brake_pads",
        value="User is considering upgrading their brake pads with the discounted set from the "
        "Winter Tune-Up event at the Speed Shop.",
        source_message_indices=[2],
    )
    (out,) = repair_citations([f], MESSAGES)
    assert out is f


def test_a_citation_that_covers_half_the_value_is_good_enough() -> None:
    f = ExtractedFact(
        memory_key="k",
        value="User replaced the spark plugs with NGK ones after a misfire and got discounted "
        "brake pads at the Winter Tune-Up event at the Speed Shop.",
        source_message_indices=[2],
    )
    (out,) = repair_citations([f], MESSAGES)
    assert out is f


def test_short_values_and_missing_messages_are_untouched() -> None:
    f = ExtractedFact(memory_key="k", value="User likes NGK.", source_message_indices=[2])
    assert repair_citations([f], MESSAGES)[0] is f
    assert repair_citations([f], None)[0] is f


@pytest.mark.asyncio
@pytest.mark.parametrize("observation_date", [None, "2024-03-01"])
async def test_extraction_repairs_citations_with_or_without_a_date(
    monkeypatch: pytest.MonkeyPatch, observation_date: str | None
) -> None:
    """Every pass after the parse reads `source_message_indices`; the repair
    runs before them whether or not the write names an observation date."""
    from server.llm.client import LLMResult
    from server.memory.app.extract import extract_from_messages

    reply = {
        "facts": [
            {
                "key": "user_spark_plug_replacement",
                "value": "User replaced the spark plugs with NGK ones after a misfire on "
                "their commute, and the engine felt smoother.",
                "relations": [{"s": "user", "v": "replaced", "o": "spark plugs"}],
                "source_message_indices": [2],
            }
        ]
    }

    class _Scripted:
        provider = "scripted"
        model = "scripted-model"

        async def complete(self, system, user, max_tokens=1024, response_format=None):
            return LLMResult(
                text=json.dumps(reply),
                tokens_in=1,
                tokens_out=1,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: _Scripted())
    result = await extract_from_messages(
        MESSAGES, judge=False, observation_date=observation_date, operator_policy=""
    )
    [fact] = [f for f in result.facts if f.memory_key.startswith("user_spark_plug")]
    assert fact.source_message_indices == [0]
