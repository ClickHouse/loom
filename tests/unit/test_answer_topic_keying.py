"""Answer-topic keying for facts the assistant supplied.

The miss class: the assistant supplies a concrete,
often STRUCTURED answer (a chord progression, a ranked list, a budget, a trail
name) to a user's factual question, and a later LOW-OVERLAP follow-up cites
only the QUESTION TOPIC ("what did you say the X was?"), never the answer
content. An answer embedded only on its value never retrieves from such a
follow-up.

The extractor emits a `question_topic` ONLY for genuine assistant-
direct-answer facts; under `memory_embed_answer_topic` that topic is PREPENDED
to the EMBEDDED text (never the verbatim value), so a topic-only follow-up
cosine-matches the answer while the structured answer stays intact.

These tests prove, without a network or API key:
  (i)  a structured assistant answer becomes a durable fact RETRIEVABLE by a
       topic-only follow-up query embedding (cosine lift vs no-topic baseline);
  (ii) verbatim preservation — the answer value/excerpt is byte-identical
       (no de-atomization of the sequence/list);
  (iii) precision guard — assistant chatter produces no fact and no topic;
  (iv) the topic touches ONLY the embedded text, never the stored value/excerpt
       (no memory column is mutated → no mirror-every-column exposure).
"""

from __future__ import annotations

import math

import pytest

from server.embedding.fake import FakeNGramEmbedder
from server.memory.app.extract.parse import _parse_fact_list
from server.memory.domain.embed_text import _build_embed_text
from server.memory.domain.excerpt import build_excerpt


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ------------------------------------------------------------------
# (iii) precision guard at the PARSE layer
# ------------------------------------------------------------------


def test_parse_question_topic_carried_when_present() -> None:
    raw = (
        '{"facts": [{"key": "assistant_stated_australia_capital", '
        '"value": "Assistant said the capital of Australia is Canberra.", '
        '"question_topic": "the capital of Australia"}]}'
    )
    facts = _parse_fact_list(raw)
    assert facts[0].question_topic == "the capital of Australia"


def test_parse_question_topic_defaults_empty_for_non_answer_facts() -> None:
    """A user-stated fact carries no question_topic — the field is the
    no-op default. This is the precision guard at the schema level: only
    facts the extractor explicitly tags as assistant answers get a topic."""
    raw = '{"facts": [{"key": "user_dog_name", "value": "User has a dog named Bowie."}]}'
    facts = _parse_fact_list(raw)
    assert facts[0].question_topic == ""


def test_parse_question_topic_normalizes_whitespace_and_bounds_length() -> None:
    raw = (
        '{"facts": [{"key": "k", "value": "v", '
        '"question_topic": "  the   chord    progression  "}]}'
    )
    facts = _parse_fact_list(raw)
    assert facts[0].question_topic == "the chord progression"

    long_topic = "word " * 100
    raw_long = (
        f'{{"facts": [{{"key": "k", "value": "v", "question_topic": "{long_topic.strip()}"}}]}}'
    )
    facts_long = _parse_fact_list(raw_long)
    assert len(facts_long[0].question_topic) <= 120


def test_parse_question_topic_rejects_non_string() -> None:
    raw = '{"facts": [{"key": "k", "value": "v", "question_topic": 42}]}'
    facts = _parse_fact_list(raw)
    assert facts[0].question_topic == ""


# ------------------------------------------------------------------
# (ii) + (iv) verbatim preservation; embed-only, value untouched
# ------------------------------------------------------------------


# A 13-note structured answer — the exact shape of the dominant miss.
_SEQUENCE_VALUE = (
    "Assistant suggested the C-major chord progression "
    "C - D - E - F - G - A - B - A - G - F - E - D - C "
    "(walks up and back down the scale)."
)
_TOPIC = "the chord progression you suggested"


def test_excerpt_and_value_unchanged_when_topic_keying_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The topic touches the EMBEDDED text only. The stored excerpt (display +
    BM25 + conflict-check surface) is byte-identical with the flag on or off,
    and the verbatim structured sequence is intact (NO de-atomization)."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_embed_answer_topic", False)
    excerpt_off = build_excerpt(_SEQUENCE_VALUE, memory_key="assistant_chords")
    embed_off = _build_embed_text(
        _SEQUENCE_VALUE, memory_key="assistant_chords", question_topic=_TOPIC
    )

    monkeypatch.setattr(settings, "memory_embed_answer_topic", True)
    excerpt_on = build_excerpt(_SEQUENCE_VALUE, memory_key="assistant_chords")
    embed_on = _build_embed_text(
        _SEQUENCE_VALUE, memory_key="assistant_chords", question_topic=_TOPIC
    )

    # The excerpt (the only thing persisted to a memory column) never changes.
    assert excerpt_on == excerpt_off
    # The verbatim 13-note sequence survives intact in the excerpt.
    assert "C - D - E - F - G - A - B - A - G - F - E - D - C" in excerpt_on
    # With the flag OFF, the embed text equals the clean excerpt (no prefix).
    assert embed_off == excerpt_off
    # With the flag ON, the embed text leads with the topic AND still contains
    # the full verbatim answer (additive prefix, never a rewrite).
    assert embed_on.startswith("[topic: the chord progression you suggested]")
    assert "C - D - E - F - G - A - B - A - G - F - E - D - C" in embed_on


def test_topic_keying_off_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default state: flag off → embedded text is the clean excerpt even when a
    question_topic is supplied. Single-variable A/B safety."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_embed_answer_topic", False)
    out = _build_embed_text(_SEQUENCE_VALUE, memory_key="k", question_topic=_TOPIC)
    assert out == build_excerpt(_SEQUENCE_VALUE, memory_key="k")
    assert "[topic:" not in out


def test_no_topic_no_prefix_even_with_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Precision guard at the embed layer: a fact with NO question_topic (a
    user-stated fact, a preference, an identity) gets NO prefix even when the
    flag is on — only assistant-answer facts are topic-keyed."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_embed_answer_topic", True)
    out = _build_embed_text(
        "User has a dog named Bowie.", memory_key="user_dog_name", question_topic=""
    )
    assert "[topic:" not in out
    assert out == build_excerpt("User has a dog named Bowie.", memory_key="user_dog_name")


def test_topic_already_in_value_skips_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No dilution / double-weighting: if every topic word is already present
    in the value, the prefix is skipped."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_embed_answer_topic", True)
    value = "Assistant said the recommended daily water intake is 3.7 liters."
    out = _build_embed_text(value, memory_key="k", question_topic="daily water intake")
    assert "[topic:" not in out


# ------------------------------------------------------------------
# (i) THE GATE: topic-only follow-up retrieves the structured answer
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_only_followup_retrieves_structured_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing assertion. A topic-only follow-up query — which shares
    NO vocabulary with the answer content (the chord notes) and only names the
    TOPIC — must cosine-match the stored answer fact MORE STRONGLY when the
    answer is topic-keyed than when it is embedded on its value alone.

    Uses the real deterministic FakeNGramEmbedder, so this exercises the actual
    character-trigram cosine the search path ranks on — not a mock."""
    from server.settings import settings

    embedder = FakeNGramEmbedder(dim=2048)

    # The user, looking back, mentions only the TOPIC, never the notes.
    followup_query = (
        "I'm looking back at our previous chat about practicing scales — "
        "what was the chord progression you suggested?"
    )
    query_vec = await embedder.embed(followup_query)

    # Baseline: answer embedded on its VALUE alone (topic keying OFF).
    monkeypatch.setattr(settings, "memory_embed_answer_topic", False)
    embed_text_baseline = _build_embed_text(
        _SEQUENCE_VALUE, memory_key="assistant_chords", question_topic=_TOPIC
    )
    baseline_vec = await embedder.embed(embed_text_baseline)
    baseline_cos = _cosine(query_vec, baseline_vec)

    # Fix: answer topic-keyed (topic keying ON).
    monkeypatch.setattr(settings, "memory_embed_answer_topic", True)
    embed_text_keyed = _build_embed_text(
        _SEQUENCE_VALUE, memory_key="assistant_chords", question_topic=_TOPIC
    )
    keyed_vec = await embedder.embed(embed_text_keyed)
    keyed_cos = _cosine(query_vec, keyed_vec)

    # Topic-keying must materially raise the topic-only follow-up's alignment
    # with the answer. The baseline (value-only) is weak because the follow-up
    # shares no trigrams with the note sequence.
    assert keyed_cos > baseline_cos, (
        f"topic-keying did not lift retrieval: keyed={keyed_cos:.4f} baseline={baseline_cos:.4f}"
    )
    # And the lift is not marginal — the topic words ("chord progression
    # suggested") are exactly the follow-up's content words.
    assert keyed_cos - baseline_cos > 0.05


# ------------------------------------------------------------------
# (iii) precision guard — END TO END: chatter yields no fact, no topic
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assistant_chatter_produces_no_topic_keyed_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-behaved extractor returns ONLY the concrete answer as a fact
    (with a topic) and emits NO fact for pleasantries / hedges. We assert the
    parse + flow keeps non-answer turns out of the topic-keyed plane.

    This guards against the CHANGE-1 failure mode (over-extraction diluting the
    pool): the topic field must not become a license to manufacture facts from
    assistant chatter."""
    from server.llm.client import LLMResult

    # A model that (correctly) returns exactly one fact for the concrete
    # answer and refuses to manufacture a fact for "Great question! / happy to
    # help" filler — and stamps the answer with a topic.
    extraction_payload = (
        '{"facts": [{"key": "assistant_stated_australia_capital", '
        '"value": "Assistant said the capital of Australia is Canberra.", '
        '"tags": ["answer", "geography"], '
        '"question_topic": "the capital of Australia"}]}'
    )

    class FakeAnswerClient:
        provider = "fake-answer"
        model = "fake-answer-1"

        async def complete(
            self,
            system: str,
            user: str,
            max_tokens: int = 1024,
            response_format: dict[str, str] | None = None,
        ) -> LLMResult:
            return LLMResult(
                text=extraction_payload,
                tokens_in=10,
                tokens_out=5,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr(
        "server.memory.app.extract.client.get_llm_client",
        lambda: FakeAnswerClient(),
    )

    from server.memory.app.extract import extract_from_messages

    result = await extract_from_messages(
        [
            {"role": "user", "content": "Can you tell me the capital of Australia?"},
            {
                "role": "assistant",
                "content": "Great question! Let me think — happy to help with "
                "that. The capital of Australia is Canberra.",
            },
            {"role": "user", "content": "thanks"},
        ],
        judge=False,
    )

    # Exactly one fact — the concrete answer. No "assistant offered to help".
    answer_facts = [f for f in result.facts if f.question_topic]
    assert len(answer_facts) == 1
    assert answer_facts[0].question_topic == "the capital of Australia"
    assert "Canberra" in answer_facts[0].value
    # No chatter fact leaked in.
    for f in result.facts:
        assert "offered to help" not in f.value.lower()
        assert "great question" not in f.value.lower()
