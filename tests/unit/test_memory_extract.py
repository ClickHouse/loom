"""Unit tests for the memory_extract service (parser + extraction path)."""

from __future__ import annotations

import json

import pytest

from server.llm import client as llm_client_module
from server.memory.app.extract import (
    _EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_PROMPT_VERSION,
    ExtractedFact,
    ExtractionError,
    extract_from_messages,
)
from server.memory.app.extract.extract import _is_nontrivial
from server.memory.app.extract.keys import _sanitize_key
from server.memory.app.extract.parse import _parse_fact_list
from server.memory.app.extract.typed import (
    _coerce_field,
    _parse_detected_fields,
    _validate_schema,
)
from server.settings import settings


def test_parse_fact_list_preserves_source_message_indices() -> None:
    raw = '{"facts": [{"key": "k1", "value": "v1", "source_message_indices": [1, "2", 1, -1]}]}'

    facts = _parse_fact_list(raw)

    assert facts[0].source_message_indices == [1, 2]


def test_sanitize_key_clamps_to_snake_case() -> None:
    assert _sanitize_key("User Dog Name!") == "user_dog_name"
    assert _sanitize_key("UPPER_lower 123") == "upper_lower_123"
    # Empty / fully-non-alphanumeric falls back to a stable default
    assert _sanitize_key("!!!") == "extracted_fact"
    # Long inputs clamp to 64 chars
    assert len(_sanitize_key("a" * 200)) == 64


def test_parse_fact_list_strips_markdown_fences() -> None:
    raw = '```json\n{"facts": [{"key": "k1", "value": "v1", "tags": ["t"]}]}\n```'
    facts = _parse_fact_list(raw)
    assert facts == [ExtractedFact(memory_key="k1", value="v1", tags=["t"])]


def test_parse_fact_list_carries_entities() -> None:
    raw = (
        '{"facts": [{"key": "k", "value": "v", '
        '"tags": ["t"], "entities": ["Bowie", "bowie", "Alice", ""]}]}'
    )
    facts = _parse_fact_list(raw)
    assert facts == [
        ExtractedFact(memory_key="k", value="v", tags=["t"], entities=["bowie", "alice"])
    ]


def test_parse_fact_list_entities_default_empty() -> None:
    raw = '{"facts": [{"key": "k", "value": "v"}]}'
    facts = _parse_fact_list(raw)
    assert facts == [ExtractedFact(memory_key="k", value="v", tags=[], entities=[])]


def test_parse_fact_list_accepts_preamble() -> None:
    raw = 'Sure, here are the facts I extracted:\n{"facts": [{"key": "x", "value": "y"}]}'
    facts = _parse_fact_list(raw)
    assert facts == [ExtractedFact(memory_key="x", value="y", tags=[])]


def test_parse_fact_list_drops_invalid_entries() -> None:
    raw = (
        '{"facts": ['
        '{"key": "", "value": "skip me"},'
        '{"key": "ok", "value": ""},'
        '{"key": "good", "value": "kept", "tags": ["a","b","c","d","e"]}'
        "]}"
    )
    facts = _parse_fact_list(raw)
    assert facts == [ExtractedFact(memory_key="good", value="kept", tags=["a", "b", "c", "d"])]


def test_parse_fact_list_raises_on_no_json() -> None:
    with pytest.raises(ExtractionError):
        _parse_fact_list("This is just prose, no JSON here at all")


def test_parse_fact_list_raises_on_invalid_json() -> None:
    with pytest.raises(ExtractionError):
        _parse_fact_list('{"facts": [unterminated')


def test_parse_fact_list_raises_on_non_list_facts() -> None:
    with pytest.raises(ExtractionError):
        _parse_fact_list('{"facts": "not a list"}')


@pytest.mark.asyncio
async def test_extract_from_messages_empty_input_no_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty messages = no LLM call, empty result."""

    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "llm_model", "fake-echo-1")
    llm_client_module.reset()
    result = await extract_from_messages([], operator_policy="")
    assert result.facts == []
    assert result.llm_result.tokens_in == 0


def test_is_nontrivial_trivial_greetings() -> None:
    assert _is_nontrivial([{"role": "user", "content": "hi"}]) is False
    assert _is_nontrivial([{"role": "user", "content": "ok thanks"}]) is False
    assert (
        _is_nontrivial(
            [
                {"role": "user", "content": "x" * 80},
            ]
        )
        is False
    )


def test_is_nontrivial_reads_length_and_punctuation_across_turns() -> None:
    # the mark and the length may come from different turns
    assert _is_nontrivial([{"content": "Hi."}, {"content": "y" * 50}]) is True
    assert _is_nontrivial([{"content": "y" * 50}, {"content": "ok!"}]) is True
    assert _is_nontrivial([{"content": "Hi."}, {"content": "  "}]) is False
    assert _is_nontrivial([]) is False


def test_is_nontrivial_substantive_input() -> None:
    assert (
        _is_nontrivial(
            [
                {
                    "role": "user",
                    "content": "My dog Bowie is seven years old. He loves Acana kibble.",
                },
            ]
        )
        is True
    )


@pytest.mark.asyncio
async def test_extract_retries_once_on_zero_facts_from_nontrivial_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero facts on substantive input retries once with a stricter addendum."""
    from server.llm.client import LLMResult

    calls: list[dict[str, str]] = []

    class FlakyClient:
        provider = "flaky"
        model = "flaky-model"

        async def complete(
            self,
            system: str,
            user: str,
            max_tokens: int = 1024,
            response_format: dict[str, str] | None = None,
        ) -> LLMResult:
            calls.append({"system": system, "user": user})
            if len(calls) > 1:
                text = '{"facts": [{"key": "k", "value": "recovered"}]}'
            else:
                text = '{"facts": []}'
            return LLMResult(
                text=text,
                tokens_in=10,
                tokens_out=5,
                provider=self.provider,
                model=self.model,
            )

    flaky = FlakyClient()
    monkeypatch.setattr(
        "server.memory.app.extract.client.get_llm_client",
        lambda: flaky,
    )

    result = await extract_from_messages(
        [
            {
                "role": "user",
                "content": "Multi-sentence input. With more than eighty chars and "
                "punctuation. Should trigger the retry.",
            },
        ],
        judge=False,
        operator_policy="",
    )
    assert len(calls) == 2, "expected exactly one retry"
    assert "CRITICAL" in calls[1]["system"]
    assert len(result.facts) == 1
    assert result.facts[0].memory_key == "k"
    assert result.llm_result.tokens_in == 20
    assert result.llm_result.tokens_out == 10


@pytest.mark.asyncio
async def test_extract_no_retry_on_trivial_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trivial input producing zero facts must not trigger a retry."""
    from server.llm.client import LLMResult

    call_count = 0

    class CountingClient:
        provider = "counter"
        model = "counter-model"

        async def complete(
            self,
            system: str,
            user: str,
            max_tokens: int = 1024,
            response_format: dict[str, str] | None = None,
        ) -> LLMResult:
            nonlocal call_count
            call_count += 1
            return LLMResult(
                text='{"facts": []}',
                tokens_in=5,
                tokens_out=2,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr(
        "server.memory.app.extract.client.get_llm_client",
        lambda: CountingClient(),
    )

    result = await extract_from_messages([{"role": "user", "content": "hi"}], operator_policy="")
    assert call_count == 1, "expected exactly one call, no retry on 'hi'"
    assert result.facts == []


@pytest.mark.asyncio
async def test_extract_from_messages_round_trip_via_fake_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full extract path round-trips through the fake LLM (no API key)."""
    monkeypatch.setenv("LOOM_LLM_PROVIDER", "fake")

    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "llm_model", "fake-echo-1")
    llm_client_module.reset()

    result = await extract_from_messages(
        [
            {"role": "user", "content": "My dog Bowie is seven years old."},
            {"role": "assistant", "content": "Got it, noted Bowie's age."},
        ],
        operator_policy="",
    )
    assert result.facts, "expected at least one extracted fact"
    for fact in result.facts:
        assert fact.memory_key
        assert fact.value
        assert "fake-extracted" in fact.tags
    assert result.llm_result.provider == "fake"


# ============================================================
# Schema-mode (typed extraction)
# ============================================================


def test_validate_schema_rejects_unknown_type() -> None:
    with pytest.raises(ExtractionError, match="unsupported spec"):
        _validate_schema({"amount": "decimal"})


def test_validate_schema_rejects_empty() -> None:
    with pytest.raises(ExtractionError, match="at least one"):
        _validate_schema({})


def test_validate_schema_accepts_supported_types() -> None:
    _validate_schema({"a": "string", "b": "number", "c": "integer", "d": "boolean"})


def test_validate_schema_accepts_enum() -> None:
    _validate_schema({"category": ["coffee", "lunch", "dinner"]})


def test_validate_schema_rejects_empty_enum() -> None:
    with pytest.raises(ExtractionError, match="at least one allowed value"):
        _validate_schema({"category": []})


def test_coerce_field_integer_from_string() -> None:
    assert _coerce_field("42", "integer") == 42


def test_coerce_field_boolean_from_string() -> None:
    assert _coerce_field("yes", "boolean") is True
    assert _coerce_field("FALSE", "boolean") is False


def test_coerce_field_bool_is_not_integer() -> None:
    with pytest.raises(ValueError, match="bool"):
        _coerce_field(True, "integer")


def test_coerce_field_number_rejects_a_container() -> None:
    with pytest.raises(ValueError, match="list"):
        _coerce_field([1.5], "number")


def test_parse_detected_fields_keeps_schema_names() -> None:
    schema = {"name": "string", "age": "integer"}
    assert _parse_detected_fields('{"fields": ["name", "other"]}', schema) == {"name"}
    assert _parse_detected_fields('{"other": 1}', schema) == set()


@pytest.mark.parametrize("fields", ["null", "3", '"name"', '{"name": 1}'])
def test_parse_detected_fields_non_list_is_unreadable(fields: str) -> None:
    # A non-list `fields` (null, a number, a string) reads as a parse failure,
    # which the caller retries once.
    schema = {"name": "string"}
    assert _parse_detected_fields(f'{{"fields": {fields}}}', schema) is None


@pytest.mark.asyncio
async def test_judge_drops_wrong_facts_and_attaches_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judge grades each fact; WRONG dropped, CORRECT/UNCERTAIN survive."""
    from server.llm.client import LLMResult

    calls: list[str] = []

    class JudgeClient:
        provider = "judge"
        model = "judge-model"

        async def complete(
            self,
            system: str,
            user: str,
            max_tokens: int = 1024,
            response_format: dict[str, str] | None = None,
        ) -> LLMResult:
            calls.append(system)
            if "You judge" in system:
                text = (
                    '{"verdicts": ['
                    '{"key":"good","verdict":"CORRECT"},'
                    '{"key":"bad","verdict":"WRONG"}'
                    "]}"
                )
            else:
                text = (
                    '{"facts": ['
                    '{"key":"good","value":"true","tags":[],"entities":[]},'
                    '{"key":"bad","value":"fabricated","tags":[],"entities":[]}'
                    "]}"
                )
            return LLMResult(
                text=text,
                tokens_in=10,
                tokens_out=5,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr(
        "server.memory.app.extract.client.get_llm_client",
        lambda: JudgeClient(),
    )

    result = await extract_from_messages(
        [
            {
                "role": "user",
                "content": "Substantive multi-sentence input. Should not trigger retry.",
            }
        ],
        judge=True,
        operator_policy="",
    )
    assert len(result.facts) == 1
    assert result.facts[0].memory_key == "good"
    assert result.facts[0].judge_verdict == "CORRECT"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_judge_disabled_keeps_all_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """judge=False: no second call, every fact survives."""
    from server.llm.client import LLMResult

    calls: list[str] = []

    class _Client:
        provider = "ext"
        model = "ext-model"

        async def complete(
            self,
            system: str,
            user: str,
            max_tokens: int = 1024,
            response_format: dict[str, str] | None = None,
        ) -> LLMResult:
            calls.append(system)
            return LLMResult(
                text=(
                    '{"facts": [{"key":"a","value":"x","tags":[],"entities":[]}, '
                    '{"key":"b","value":"y","tags":[],"entities":[]}]}'
                ),
                tokens_in=10,
                tokens_out=5,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr(
        "server.memory.app.extract.client.get_llm_client",
        lambda: _Client(),
    )

    result = await extract_from_messages(
        [{"role": "user", "content": "Substantive input. Two facts."}],
        judge=False,
        operator_policy="",
    )
    assert len(result.facts) == 2
    assert len(calls) == 1  # extract only, no judge


# ============================================================
# Extraction-completeness prompt coverage
# ============================================================


def test_prompt_version_is_pinned() -> None:
    assert EXTRACTION_PROMPT_VERSION == "extraction-v1"


def test_prompt_has_assistant_recommendation_example() -> None:
    assert "EXAMPLE 8b" in _EXTRACTION_SYSTEM_PROMPT
    assert "assistant_recommended" in _EXTRACTION_SYSTEM_PROMPT
    assert "hydro coach" in _EXTRACTION_SYSTEM_PROMPT


def test_prompt_has_secondary_clause_example() -> None:
    assert "EXAMPLE 9b" in _EXTRACTION_SYSTEM_PROMPT
    assert "secondary clause" in _EXTRACTION_SYSTEM_PROMPT.lower()


def test_prompt_has_money_raised_and_occasion_derived_examples() -> None:
    assert "EXAMPLE 23" in _EXTRACTION_SYSTEM_PROMPT
    assert "EXAMPLE 24" in _EXTRACTION_SYSTEM_PROMPT
    assert "EXAMPLE 25" in _EXTRACTION_SYSTEM_PROMPT
    assert '"predicate":"raise"' in _EXTRACTION_SYSTEM_PROMPT
    assert '"predicate":"attend"' in _EXTRACTION_SYSTEM_PROMPT


def test_prompt_charity_amount_uses_numeric_value_and_stable_category() -> None:
    assert '"numeric_value":450' in _EXTRACTION_SYSTEM_PROMPT
    assert '"category":"charity"' in _EXTRACTION_SYSTEM_PROMPT
    assert '"unit":"USD"' in _EXTRACTION_SYSTEM_PROMPT
    assert "double-counts" in _EXTRACTION_SYSTEM_PROMPT.lower()


def test_prompt_dinner_gathering_is_attend_occurrence() -> None:
    assert '"category":"dinner party"' in _EXTRACTION_SYSTEM_PROMPT
    assert "potluck" in _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "no derived record" in _EXTRACTION_SYSTEM_PROMPT.lower()


def test_prompt_pending_intention_is_not_an_occurrence() -> None:
    lowered = _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "intention is not a dated occurrence" in lowered


def test_parse_charity_amount_derived_carries_numeric_value() -> None:
    raw = (
        '{"facts": [{"key": "user_bake_sale", '
        '"value": "User raised $450 for the children\'s hospital on 2024-03-02.", '
        '"tags": ["fundraising"], "entities": ["bake sale"], '
        '"temporal_anchor": "2024-03-02", '
        '"derived": [{"predicate": "raise", "object": "bake sale", '
        '"category": "charity", "numeric_value": 450, "unit": "USD", '
        '"occurred_at": "2024-03-02"}]}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    d = facts[0].derived
    assert len(d) == 1
    assert d[0]["numeric_value"] == 450.0
    assert d[0]["unit"] == "usd"
    assert d[0]["category"] == "charity"


def test_parse_dinner_gathering_derived_is_attend_count() -> None:
    raw = (
        '{"facts": [{"key": "user_potluck", '
        '"value": "User attended a potluck at Dan\'s on 2024-02-08.", '
        '"tags": ["social"], "entities": ["dan", "potluck"], '
        '"temporal_anchor": "2024-02-08", '
        '"derived": [{"predicate": "attend", "object": "dan\'s potluck", '
        '"category": "dinner party", "numeric_value": null, "unit": "count", '
        '"occurred_at": "2024-02-08"}]}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    d = facts[0].derived
    assert len(d) == 1
    assert d[0]["numeric_value"] is None
    assert d[0]["unit"] == "count"
    assert d[0]["category"] == "dinner party"


def test_parse_assistant_recommendation_shape() -> None:
    raw = (
        '{"facts": [{"key": "assistant_recommended_waterminder", '
        '"value": "Assistant recommended the WaterMinder app.", '
        '"tags": ["recommendation", "app"], "entities": ["waterminder"], '
        '"relations": [{"s": "assistant", "v": "recommended", "o": "waterminder"}], '
        '"source_message_indices": [1]}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.memory_key == "assistant_recommended_waterminder"
    assert fact.entities == ["waterminder"]
    assert fact.source_message_indices == [1]
    assert fact.relations == [("assistant", "recommend", "waterminder")]


# ----------------------------------------------------------------------
# ss-assistant direct-answer recall (Examples 8c / 8d)
# ----------------------------------------------------------------------


def test_prompt_has_assistant_direct_answer_example() -> None:
    assert "EXAMPLE 8c" in _EXTRACTION_SYSTEM_PROMPT
    lower = _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "direct answer" in lower
    assert "3.7 liters" in _EXTRACTION_SYSTEM_PROMPT
    assert "2.7 liters" in _EXTRACTION_SYSTEM_PROMPT
    assert '{"s":"assistant","v":"stated","o":"daily water intake"}' in (_EXTRACTION_SYSTEM_PROMPT)


def test_prompt_has_assistant_chatter_precision_guard() -> None:
    assert "EXAMPLE 8d" in _EXTRACTION_SYSTEM_PROMPT
    lower = _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "precision guard" in lower
    assert "Canberra" in _EXTRACTION_SYSTEM_PROMPT
    assert "great question" in lower
    assert "do not manufacture" in lower or "would only dilute" in lower


def test_parse_assistant_direct_answer_shape() -> None:
    raw = (
        '{"facts": [{"key": "assistant_stated_daily_water_intake", '
        '"value": "Assistant said the recommended daily water intake is about '
        "3.7 liters per day for men and 2.7 liters for women, per the U.S. "
        'National Academies guideline.", '
        '"tags": ["answer", "health"], "entities": ["national academies"], '
        '"relations": [{"s": "assistant", "v": "stated", "o": "daily water intake"}], '
        '"source_message_indices": [1]}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.memory_key == "assistant_stated_daily_water_intake"
    assert "3.7 liters" in fact.value
    assert "2.7 liters" in fact.value
    assert fact.relations[0][0] == "assistant"
    assert fact.relations[0][2] == "daily water intake"
    assert fact.source_message_indices == [1]


@pytest.mark.asyncio
async def test_extract_assistant_recommendation_no_ack_yields_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assistant recommendation with no user ack yields a subject=assistant fact."""
    from server.llm.client import LLMResult

    captured_system: list[str] = []

    class _Scripted:
        provider = "scripted"
        model = "scripted-model"

        async def complete(self, system, user, max_tokens=1024, response_format=None):
            captured_system.append(system)
            return LLMResult(
                text=(
                    '{"facts": [{"key": "assistant_recommended_waterminder", '
                    '"value": "Assistant recommended the WaterMinder app for '
                    'tracking water intake.", '
                    '"tags": ["recommendation", "app"], "entities": ["waterminder"], '
                    '"relations": [{"s": "assistant", "v": "recommended", '
                    '"o": "waterminder"}], "source_message_indices": [1]}]}'
                ),
                tokens_in=1,
                tokens_out=1,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: _Scripted())
    result = await extract_from_messages(
        [
            {"role": "user", "content": "Any good apps for tracking my water intake?"},
            {
                "role": "assistant",
                "content": "I'd recommend WaterMinder — it lets you set daily "
                "goals and sends reminders.",
            },
        ],
        judge=False,
        operator_policy="",
    )
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert "waterminder" in fact.value.lower()
    assert fact.relations[0][0] == "assistant"
    assert fact.entities == ["waterminder"]
    assert "EXAMPLE 8c" in captured_system[0]


def test_parse_assistant_pleasantry_no_fact() -> None:
    """A content-free assistant pleasantry produces no fact."""
    raw = '{"facts": []}'
    facts = _parse_fact_list(raw)
    assert facts == []


def test_parse_multi_clause_two_facts() -> None:
    raw = (
        '{"facts": ['
        '{"key": "user_beans_purchase", "value": "User bought dark-roast beans for $18.", '
        '"entities": ["dark-roast beans"]}, '
        '{"key": "user_dripper_purchase", "value": "User bought a pour-over dripper for $32.", '
        '"entities": ["pour-over dripper"]}'
        "]}"
    )
    facts = _parse_fact_list(raw)
    assert [f.memory_key for f in facts] == [
        "user_beans_purchase",
        "user_dripper_purchase",
    ]
    assert "$18" in facts[0].value
    assert "$32" in facts[1].value


def test_parse_derived_money_raised_occurrence() -> None:
    raw = (
        '{"facts": [{"key": "user_bake_sale", '
        '"value": "User raised $450 at a bake sale on 2024-03-02.", '
        '"temporal_anchor": "2024-03-02", '
        '"derived": [{"predicate": "raise", "object": "bake sale", '
        '"category": "fundraising", "numeric_value": 450, "unit": "USD", '
        '"occurred_at": "2024-03-02"}]}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    derived = facts[0].derived
    assert len(derived) == 1
    rec = derived[0]
    assert rec["predicate"] == "rais"
    assert rec["numeric_value"] == 450.0
    assert rec["unit"] == "usd"
    assert rec["occurred_at"] == "2024-03-02"


def test_parse_derived_occasion_attended_occurrence() -> None:
    raw = (
        '{"facts": [{"key": "user_party", '
        '"value": "User attended Mark\'s housewarming party on 2023-11-17.", '
        '"temporal_anchor": "2023-11-17", '
        '"derived": [{"predicate": "attend", "object": "mark\'s housewarming party", '
        '"category": "party", "numeric_value": null, "unit": "count", '
        '"occurred_at": "2023-11-17"}]}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    rec = facts[0].derived[0]
    assert rec["predicate"] == "attend"
    assert rec["numeric_value"] is None
    assert rec["unit"] == "count"
    assert rec["occurred_at"] == "2023-11-17"


# ----------------------------------------------------------------------
# ADD-only state-change discipline (prompt structure)
# ----------------------------------------------------------------------


def test_prompt_has_add_only_state_change_rule() -> None:
    upper = _EXTRACTION_SYSTEM_PROMPT
    lower = upper.lower()
    assert "STATE CHANGES ARE ADD-ONLY" in upper
    assert "never average" in lower
    assert "do not invent or compute a consolidated value" in lower
    assert "do not drop the new value" in lower
    assert "keep the prior fact too" in lower


def test_prompt_has_state_change_worked_examples() -> None:
    upper = _EXTRACTION_SYSTEM_PROMPT
    assert "EXAMPLE 26" in upper
    assert "EXAMPLE 27" in upper
    assert "EXAMPLE 28" in upper
    assert "NO invented" in upper or 'is NOT "12"' in upper


def test_prompt_distinguishes_event_from_mention_date() -> None:
    lower = _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "event date vs mention/booking date" in lower
    assert "anchor march 3" in lower
    assert "re-mention of a past event keeps the original event date" in lower


def test_prompt_broadens_anchor_to_ambient_and_named_month() -> None:
    lower = _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "ambient observation" in lower
    assert "named-month" in lower or "named month" in lower
    assert "EXAMPLE 29" in _EXTRACTION_SYSTEM_PROMPT
    assert "EXAMPLE 30" in _EXTRACTION_SYSTEM_PROMPT
    assert "EXAMPLE 31" in _EXTRACTION_SYSTEM_PROMPT


def test_prompt_resolves_against_observation_not_ingestion() -> None:
    lower = _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "never against the current wall-clock" in lower


# ----------------------------------------------------------------------
# duration / "how long" extraction recall (Examples 32/32b/33)
# ----------------------------------------------------------------------


def test_prompt_has_duration_start_anchor_example() -> None:
    assert "EXAMPLE 32" in _EXTRACTION_SYSTEM_PROMPT
    lower = _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "elapsed-duration operand" in lower
    assert "user_comedy_specials_start" in _EXTRACTION_SYSTEM_PROMPT
    assert '"temporal_anchor":"2024-03-01"' in _EXTRACTION_SYSTEM_PROMPT
    assert '"predicate":"start"' in _EXTRACTION_SYSTEM_PROMPT


def test_prompt_has_explicit_duration_example() -> None:
    assert "EXAMPLE 32b" in _EXTRACTION_SYSTEM_PROMPT
    assert '"numeric_value":6' in _EXTRACTION_SYSTEM_PROMPT
    assert '"unit":"weeks"' in _EXTRACTION_SYSTEM_PROMPT
    assert "about six weeks" in _EXTRACTION_SYSTEM_PROMPT


def test_prompt_has_duration_precision_guard() -> None:
    assert "EXAMPLE 33" in _EXTRACTION_SYSTEM_PROMPT
    lower = _EXTRACTION_SYSTEM_PROMPT.lower()
    assert "precision guard for durations" in lower
    assert "do not manufacture" in lower
    assert "user_comedy_specials_preference" in _EXTRACTION_SYSTEM_PROMPT


def test_parse_duration_start_anchor_shape() -> None:
    raw = (
        '{"facts": [{"key": "user_comedy_specials_start", '
        '"value": "User started watching stand-up comedy specials regularly in '
        'March 2024 (~2024-03-01).", '
        '"tags": ["hobby", "date"], "entities": ["stand-up comedy specials"], '
        '"relations": [{"s": "user", "v": "started_watching", '
        '"o": "stand-up comedy specials"}], '
        '"temporal_anchor": "2024-03-01", '
        '"derived": [{"predicate": "start", "object": "stand-up comedy specials", '
        '"category": "hobby", "numeric_value": null, "unit": "count", '
        '"occurred_at": "2024-03-01"}]}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.temporal_anchor == "2024-03-01T00:00:00"
    assert "started watching" in fact.value.lower()
    assert fact.derived[0]["predicate"] == "start"


def test_parse_explicit_duration_carries_numeric_value() -> None:
    raw = (
        '{"facts": [{"key": "user_binoculars_usage_duration", '
        '"value": "User had been using their binoculars for about six weeks '
        'before the lens fogged up.", '
        '"tags": ["possession", "duration"], "entities": ["binoculars"], '
        '"temporal_anchor": "2023-10-15", '
        '"derived": [{"predicate": "use", "object": "binoculars", '
        '"category": "equipment", "numeric_value": 6, "unit": "weeks", '
        '"occurred_at": "2023-10-15"}]}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.derived[0]["numeric_value"] == 6
    assert fact.derived[0]["unit"] == "weeks"
    assert "about six weeks" in fact.value


@pytest.mark.asyncio
async def test_extract_duration_start_yields_anchored_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A started-activity transcript yields a fact carrying the START anchor."""
    from server.llm.client import LLMResult

    captured_system: list[str] = []

    class _Scripted:
        provider = "scripted"
        model = "scripted-model"

        async def complete(self, system, user, max_tokens=1024, response_format=None):
            captured_system.append(system)
            return LLMResult(
                text=(
                    '{"facts": [{"key": "user_comedy_specials_start", '
                    '"value": "User started watching stand-up comedy specials '
                    'regularly in March 2024 (~2024-03-01).", '
                    '"tags": ["hobby", "date"], '
                    '"entities": ["stand-up comedy specials"], '
                    '"relations": [{"s": "user", "v": "started_watching", '
                    '"o": "stand-up comedy specials"}], '
                    '"temporal_anchor": "2024-03-01", '
                    '"derived": [{"predicate": "start", '
                    '"object": "stand-up comedy specials", "category": "hobby", '
                    '"numeric_value": null, "unit": "count", '
                    '"occurred_at": "2024-03-01"}]}]}'
                ),
                tokens_in=1,
                tokens_out=1,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: _Scripted())
    result = await extract_from_messages(
        [
            {
                "role": "user",
                "content": "I started watching stand-up comedy specials "
                "regularly back in March — been hooked ever since.",
            },
        ],
        observation_date="2024-06-10",
        judge=False,
        operator_policy="",
    )
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.temporal_anchor == "2024-03-01T00:00:00"
    assert "started watching" in fact.value.lower()
    assert "EXAMPLE 32" in captured_system[0]


@pytest.mark.asyncio
async def test_extract_no_stated_duration_does_not_manufacture_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stated start/duration yields a timeless preference, no anchor/derived."""
    from server.llm.client import LLMResult

    class _Scripted:
        provider = "scripted"
        model = "scripted-model"

        async def complete(self, system, user, max_tokens=1024, response_format=None):
            return LLMResult(
                text=(
                    '{"facts": [{"key": "user_comedy_specials_preference", '
                    '"value": "User watches a lot of stand-up comedy specials '
                    'as their go-to after a long day.", '
                    '"tags": ["preference", "hobby", '
                    '"preference:entertainment"], '
                    '"entities": ["stand-up comedy specials"]}]}'
                ),
                tokens_in=1,
                tokens_out=1,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: _Scripted())
    result = await extract_from_messages(
        [
            {
                "role": "user",
                "content": "I watch a lot of stand-up comedy specials — "
                "they're my go-to after a long day.",
            },
        ],
        observation_date="2024-06-10",
        judge=False,
        operator_policy="",
    )
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.temporal_anchor is None
    assert fact.derived == []
    assert "week" not in fact.value.lower()
    assert "march" not in fact.value.lower()


# ----------------------------------------------------------------------
# parser round-trip + de-atomization guard
# ----------------------------------------------------------------------


def test_parse_state_change_emits_both_values_as_separate_facts() -> None:
    raw = (
        '{"facts": ['
        '{"key": "user_crash_course_progress",'
        ' "value": "User has watched 10 Crash Course videos (as of 2023-08-11).",'
        ' "temporal_anchor": "2023-08-11"},'
        '{"key": "user_crash_course_progress_update",'
        ' "value": "User has now watched 15 Crash Course videos (as of 2023-09-30).",'
        ' "temporal_anchor": "2023-09-30", "linked_keys": []}'
        "]}"
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 2
    values = " ".join(f.value for f in facts)
    assert "10 Crash Course videos" in values
    assert "15 Crash Course videos" in values
    assert "12" not in values
    anchors = {f.temporal_anchor for f in facts}
    assert anchors == {"2023-08-11T00:00:00", "2023-09-30T00:00:00"}


def test_parse_preserves_atomic_fact_text_verbatim() -> None:
    """The parser is a pure pass-through for the fact `value` — no rewriting."""
    atomic = "User now sees their therapist every week (as of 2024-02-01)."
    raw = json.dumps(
        {
            "facts": [
                {
                    "key": "user_therapy_frequency_update",
                    "value": atomic,
                    "temporal_anchor": "2024-02-01",
                }
            ]
        }
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    assert facts[0].value == atomic


def test_parse_multivalued_set_not_collapsed() -> None:
    """Three hobbies stated together parse as three distinct facts."""
    raw = (
        '{"facts": ['
        '{"key": "user_hobby_climbing", "value": "User enjoys rock climbing."},'
        '{"key": "user_hobby_pottery", "value": "User enjoys pottery."},'
        '{"key": "user_hobby_cycling", "value": "User enjoys cycling."}'
        "]}"
    )
    facts = _parse_fact_list(raw)
    assert [f.memory_key for f in facts] == [
        "user_hobby_climbing",
        "user_hobby_pottery",
        "user_hobby_cycling",
    ]


def test_parse_stated_total_value_survives_verbatim() -> None:
    """A stated current total survives verbatim, not a derived count."""
    raw = (
        '{"facts": [{"key": "user_houseplant_count",'
        ' "value": "User now has five houseplants (as of 2024-06-02)."}]}'
    )
    facts = _parse_fact_list(raw)
    assert len(facts) == 1
    assert "five houseplants" in facts[0].value
    assert facts[0].derived == []


# ----------------------------------------------------------------------
# backstop anchors ambient observations + named months without de-atomizing
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backstop_anchors_ambient_observation_without_touching_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient observation gets a backstop anchor; value text is untouched."""
    from server.memory.app.extract import extract_from_messages

    class _Scripted:
        provider = "scripted"
        model = "scripted-model"

        async def complete(self, system, user, max_tokens=1024, response_format=None):
            from server.llm.client import LLMResult

            return LLMResult(
                text=(
                    '{"facts": [{"key": "k1",'
                    ' "value": "User noticed the goldfinches returned about a week ago."}]}'
                ),
                tokens_in=1,
                tokens_out=1,
                provider=self.provider,
                model=self.model,
            )

    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: _Scripted())
    result = await extract_from_messages(
        [{"role": "user", "content": "goldfinches"}],
        observation_date="2023-05-20",
        judge=False,
        operator_policy="",
    )
    f = result.facts[0]
    # obs 2023-05-20, "about a week ago" -> 2023-05-13
    assert f.temporal_anchor == "2023-05-13T00:00:00"
    assert f.value == "User noticed the goldfinches returned about a week ago."


# ----------------------------------------------------------------------
# Discards are counted, never silent
# ----------------------------------------------------------------------


def test_parse_fact_list_reports_discards() -> None:
    from server.memory.app.extract.parse import _parse_fact_list

    discards: list[str] = []
    facts = _parse_fact_list(
        '{"facts": [{"key": "a"}, {"key": "b", "value": "v"}, 5, {"value": "orphan"}]}',
        discards=discards,
    )
    assert [f.memory_key for f in facts] == ["b"]
    assert len(discards) == 3
    assert discards[0].startswith("missing_value:a")
    assert discards[1].startswith("not_an_object")
    assert discards[2].startswith("missing_key")


@pytest.mark.asyncio
async def test_judge_wrong_drop_is_counted_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from server.llm.client import LLMResult

    class JudgeClient:
        provider = "judge"
        model = "judge-model"

        async def complete(
            self,
            system: str,
            user: str,
            max_tokens: int = 1024,
            response_format: dict[str, str] | None = None,
        ) -> LLMResult:
            if "You judge" in system:
                text = (
                    '{"verdicts": ['
                    '{"key":"good","verdict":"CORRECT"},'
                    '{"key":"bad","verdict":"WRONG"}'
                    "]}"
                )
            else:
                text = (
                    '{"facts": ['
                    '{"key":"good","value":"true","tags":[],"entities":[]},'
                    '{"key":"bad","value":"fabricated","tags":[],"entities":[]},'
                    '{"key":"","value":"no key"}'
                    "]}"
                )
            return LLMResult(
                text=text, tokens_in=10, tokens_out=5, provider=self.provider, model=self.model
            )

    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: JudgeClient())
    with caplog.at_level(logging.INFO, logger="server.memory.app.extract"):
        result = await extract_from_messages(
            [
                {
                    "role": "user",
                    "content": "Substantive multi-sentence input. Should not trigger retry.",
                }
            ],
            judge=True,
            operator_policy="",
        )
    assert [f.memory_key for f in result.facts] == ["good"]
    assert result.dropped_judge_wrong == 1
    assert result.dropped_parse == 1
    assert any("judge dropped fact 'bad'" in rec.getMessage() for rec in caplog.records)


def test_extraction_prompt_asks_for_run_outcomes() -> None:
    # A run trace's decided values are facts; the request alone
    # is a plan. On MemoryArena travel traces own-choice venues kept went
    # 43% → 76% with the rule (31 traces).
    from server.memory.domain.extraction_prompts import _EXTRACTION_SYSTEM_PROMPT

    assert "Outcomes of a run" in _EXTRACTION_SYSTEM_PROMPT
    assert "every DECIDED value" in _EXTRACTION_SYSTEM_PROMPT
