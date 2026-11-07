from typing import Any

from server.memory.domain.extracted_facts import _source_role_tags


def test_source_role_tags_single_role() -> None:
    messages = [
        {"role": "user", "content": "I like dark mode"},
        {"role": "assistant", "content": "Noted"},
    ]
    fact = {"source_message_indices": [0, 0]}

    assert _source_role_tags(messages, fact) == ["source_role:user"]


def test_source_role_tags_mixed_roles() -> None:
    messages = [
        {"role": "user", "content": "I like dark mode"},
        {"role": "assistant", "content": "I will use dark mode"},
    ]
    fact = {"source_message_indices": [0, 1]}

    assert _source_role_tags(messages, fact) == [
        "source_role:mixed",
        "source_role:user",
        "source_role:assistant",
    ]


def test_source_role_tags_ignores_invalid_indices() -> None:
    messages = [{"role": "user", "content": "I like dark mode"}]
    fact = {"source_message_indices": [99, "bad"]}

    # Invalid indices bind to no role; the fact is labelled unattributed
    # rather than left silent.
    assert _source_role_tags(messages, fact) == ["source_role:unattributed"]


# ----------------------------------------------------------------------
# valid_at defaulting in _step_bulk_write
# ----------------------------------------------------------------------
# Valid-time must default to the conversation's observation date (or the
# fact's own non-future anchor) so every extracted memory is dated for
# chronological grouping; rows are undated only when the caller supplied
# no observation_date at all.

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

from server.memory.app.pipeline import _step_bulk_write  # noqa: E402
from server.memory.app.pipeline_state import WorkflowState  # noqa: E402


class _BulkCapture:
    def __init__(self) -> None:
        self.inputs: list = []

    async def __call__(self, bulk_input):
        self.inputs.append(bulk_input)

        class _Out:
            written: list = []
            skipped_blacklisted: list = []

        return _Out()


async def _run_bulk_write(
    monkeypatch: pytest.MonkeyPatch,
    fact: dict,
    *,
    observation_date: str | None = None,
    state_valid_at: datetime | None = None,
):
    from server.memory.app import bulk_set as bulk_set_mod

    capture = _BulkCapture()
    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", capture)
    state = WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        observation_date=observation_date,
        valid_at=state_valid_at,
        extracted_facts=[fact],
    )
    await _step_bulk_write(state, {})
    assert capture.inputs, "bulk write was not invoked"
    return capture.inputs[0].entries[0]


@pytest.mark.asyncio
async def test_valid_at_defaults_to_observation_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _run_bulk_write(
        monkeypatch,
        {"key": "k1", "value": "User went hiking."},
        observation_date="2023-05-07",
    )
    assert entry.valid_at == datetime(2023, 5, 7)


@pytest.mark.asyncio
async def test_valid_at_prefers_past_anchor_over_observation_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fact's own anchor (the date it is ABOUT) is finer than the
    conversation date for a reported past event."""
    entry = await _run_bulk_write(
        monkeypatch,
        {
            "key": "k1",
            "value": "User bought a grill on 2023-03-15.",
            "temporal_anchor": "2023-03-15",
        },
        observation_date="2023-04-01",
    )
    assert entry.valid_at == datetime(2023, 3, 15)


@pytest.mark.asyncio
async def test_valid_at_ignores_future_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planned-event anchor must not future-date validity: the fact about
    the plan became true when stated."""
    entry = await _run_bulk_write(
        monkeypatch,
        {
            "key": "k1",
            "value": "User has a trip planned for 2026-08-12.",
            "temporal_anchor": "2026-08-12",
        },
        observation_date="2023-04-01",
    )
    assert entry.valid_at == datetime(2023, 4, 1)


@pytest.mark.asyncio
async def test_valid_at_extractor_value_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _run_bulk_write(
        monkeypatch,
        {
            "key": "k1",
            "value": "User moved to NYC in 2022.",
            "valid_at": "2022-06-01",
            "temporal_anchor": "2022-06-01",
        },
        observation_date="2023-04-01",
    )
    assert entry.valid_at == datetime(2022, 6, 1)


@pytest.mark.asyncio
async def test_valid_at_caller_default_wins_over_observation_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _run_bulk_write(
        monkeypatch,
        {"key": "k1", "value": "User went hiking."},
        observation_date="2023-05-07",
        state_valid_at=datetime(2023, 1, 1),
    )
    assert entry.valid_at == datetime(2023, 1, 1)


@pytest.mark.asyncio
async def test_valid_at_stays_null_without_observation_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _run_bulk_write(
        monkeypatch,
        {"key": "k1", "value": "User went hiking."},
    )
    assert entry.valid_at is None


# ----------------------------------------------------------------------
# memory_type is derived from the extractor's tags, not hard-coded.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_type_derived_from_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    pref = await _run_bulk_write(
        monkeypatch, {"key": "k", "value": "v", "tags": ["preference", "tooling"]}
    )
    assert pref.memory_type == "preference"

    decision = await _run_bulk_write(
        monkeypatch, {"key": "k", "value": "chose ClickHouse over X", "tags": ["decision"]}
    )
    assert decision.memory_type == "decision"

    plain = await _run_bulk_write(monkeypatch, {"key": "k", "value": "has a dog", "tags": ["pet"]})
    assert plain.memory_type == "semantic"


# ----------------------------------------------------------------------
# Fail-closed extraction: ExtractionError must NOT be swallowed by the
# llm_extract step. Swallowing it would return a zero-fact state and the
# handler would answer HTTP 200 with written=[] — the transcript lost
# silently, contradicting the extractor's documented contract.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_extract_step_propagates_extraction_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract as mx
    from server.memory.app.pipeline import _step_llm_extract

    async def _boom(*args, **kwargs):
        raise mx.ExtractionError("no JSON object in LLM response")

    monkeypatch.setattr(mx, "extract_from_messages", _boom)
    state = WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        messages=[{"role": "user", "content": "I moved to Lisbon in 2021."}],
    )
    with pytest.raises(mx.ExtractionError):
        await _step_llm_extract(state, {})
    # Nothing was recorded as a soft error — this is a hard failure.
    assert state.errors == []
    assert state.extracted_facts == []


@pytest.mark.asyncio
async def test_run_pipeline_aborts_on_extraction_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pipeline runner must not turn the fail-closed step into a
    fail-open one: downstream steps do not run, the error reaches the
    caller (the handler maps it to 502)."""
    import server.memory.app.extract as mx
    from server.memory.app.pipeline import register_step, run_pipeline
    from server.memory.app.pipeline_state import PipelineStep

    async def _boom(*args, **kwargs):
        raise mx.ExtractionError("invalid JSON")

    monkeypatch.setattr(mx, "extract_from_messages", _boom)
    ran: list[str] = []

    async def _sentinel(state, config):
        ran.append("after")
        return state

    register_step("test.after_extract", _sentinel)
    state = WorkflowState(
        org="dev", namespace="ns", agent="a", messages=[{"role": "user", "content": "x"}]
    )
    with pytest.raises(mx.ExtractionError):
        await run_pipeline(
            [PipelineStep("loom.llm_extract"), PipelineStep("test.after_extract")],
            state,
        )
    assert ran == []


# ----------------------------------------------------------------------
# Key collisions: when two facts' keys sanitise to the same key, the second
# is disambiguated deterministically (never dropped) and the collision is
# counted.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_write_disambiguates_colliding_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import bulk_set as bulk_set_mod

    capture = _BulkCapture()
    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", capture)
    state = WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        extracted_facts=[
            {"key": "user_city", "value": "User lives in Lisbon."},
            {"key": "user_city", "value": "User's partner lives in Porto."},
            {"key": "user_city", "value": "User grew up in Braga."},
        ],
    )
    await _step_bulk_write(state, {})
    keys = [e.memory_key for e in capture.inputs[0].entries]
    assert keys == ["user_city", "user_city_2", "user_city_3"]
    values = [e.value for e in capture.inputs[0].entries]
    assert "User's partner lives in Porto." in values
    assert state.key_collisions == 2


def test_disambiguate_key_respects_length_cap() -> None:
    from server.memory.domain.extracted_facts import _KEY_MAX_LEN, _disambiguate_key

    long_key = "k" * _KEY_MAX_LEN
    out = _disambiguate_key(long_key, {long_key})
    assert len(out) <= _KEY_MAX_LEN
    assert out.endswith("_2")
    # Deterministic and skips taken candidates.
    assert _disambiguate_key("a", {"a", "a_2"}) == "a_3"


@pytest.mark.asyncio
async def test_llm_extract_step_copies_drop_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract as mx
    from server.llm.client import LLMResult
    from server.memory.app.pipeline import _step_llm_extract

    async def _fake(*args, **kwargs):
        return mx.ExtractionResult(
            facts=[],
            llm_result=LLMResult(text="", tokens_in=1, tokens_out=1, provider="p", model="m"),
            dropped_parse=2,
            dropped_judge_wrong=1,
        )

    monkeypatch.setattr(mx, "extract_from_messages", _fake)
    state = WorkflowState(
        org="dev", namespace="ns", agent="a", messages=[{"role": "user", "content": "x"}]
    )
    await _step_llm_extract(state, {})
    assert state.dropped_parse == 2
    assert state.dropped_judge_wrong == 1


# ---------------------------------------------------------------- foresight


@pytest.mark.asyncio
async def test_foresight_until_types_the_row_and_bounds_valid_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _run_bulk_write(
        monkeypatch,
        {
            "key": "user_lisbon_trip",
            "value": "User flies to Lisbon on 2025-10-20 for a week.",
            "tags": ["travel", "plan"],
            "temporal_anchor": "2025-10-20",
            "foresight": {"until": "2025-10-27", "duration_days": 7},
        },
        observation_date="2025-10-06",
    )
    assert entry.memory_type == "foresight"
    assert entry.valid_to == datetime(2025, 10, 27)
    assert "foresight" in entry.tags and "plan" in entry.tags
    # a plan is true from when it was stated, not the planned date
    assert entry.valid_at == datetime(2025, 10, 6)


@pytest.mark.asyncio
async def test_foresight_duration_counts_from_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _run_bulk_write(
        monkeypatch,
        {
            "key": "user_antibiotics_course",
            "value": "User is on a two-week course of antibiotics.",
            "tags": ["health"],
            "valid_at": "2025-10-05",
            "foresight": {"until": None, "duration_days": 14},
        },
        observation_date="2025-10-06",
    )
    assert entry.memory_type == "foresight" and entry.valid_to == datetime(2025, 10, 19)


@pytest.mark.asyncio
async def test_foresight_never_widens_an_extractor_valid_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _run_bulk_write(
        monkeypatch,
        {
            "key": "k",
            "value": "v",
            "valid_to": "2025-10-10",
            "foresight": {"until": "2025-10-27"},
        },
        observation_date="2025-10-06",
    )
    assert entry.memory_type == "foresight" and entry.valid_to == datetime(2025, 10, 10)


@pytest.mark.asyncio
async def test_malformed_or_empty_foresight_leaves_the_row_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in (
        {"until": None, "duration_days": None},
        {"duration_days": -3},
        "soon",
        [],
        {"duration_days": "x"},
    ):
        entry = await _run_bulk_write(
            monkeypatch,
            {"key": "k", "value": "v", "tags": ["preference"], "foresight": bad},
            observation_date="2025-10-06",
        )
        assert entry.memory_type == "preference" and entry.valid_to is None, bad
    # a duration with no start (no observation date, no valid_at) is not a window
    entry = await _run_bulk_write(
        monkeypatch, {"key": "k", "value": "v", "foresight": {"duration_days": 3}}
    )
    assert entry.memory_type == "semantic" and entry.valid_to is None


def test_foresight_end_helper() -> None:
    from server.memory.domain.extracted_facts import foresight_end

    assert foresight_end({"until": "2025-01-31"}, start=None) == datetime(2025, 1, 31)
    assert foresight_end({"duration_days": 2}, start=datetime(2025, 1, 1)) == datetime(2025, 1, 3)
    assert foresight_end({"duration_days": 2}, start=None) is None
    assert foresight_end(None, start=datetime(2025, 1, 1)) is None


def test_foresight_count_reaches_the_output_and_the_ledger() -> None:
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.observability.event_args import project_result

    assert project_result(MemorySetFromMessagesOutput(foresight=2))["out_foresight"] == "2"


# ---------------------------------------------------------------- ownership + forget


@pytest.mark.asyncio
async def test_about_other_rides_the_row_as_a_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = await _run_bulk_write(
        monkeypatch,
        {
            "key": "user_sister_priya_caffeine",
            "value": "Priya takes decaf.",
            "tags": ["family"],
            "about": "other",
        },
        observation_date="2025-11-02",
    )
    assert "about:other" in entry.tags and entry.memory_type == "semantic"
    entry2 = await _run_bulk_write(
        monkeypatch, {"key": "k", "value": "v", "tags": ["preference"], "about": "user"}
    )
    assert "about:other" not in entry2.tags and entry2.memory_type == "preference"


def test_forget_targets_collects_own_key_and_linked_keys() -> None:
    from server.memory.domain.forget_requests import forget_targets

    facts = [
        {
            "key": "user_salary",
            "forget": True,
            "linked_keys": ["user_salary", "__idx:3", "user_income"],
        },
        {"key": "user_coffee_preference", "value": "dark roast"},
    ]
    assert forget_targets(facts) == {"user_salary", "user_income"}
    assert forget_targets([{"key": "k"}]) == set()


@pytest.mark.asyncio
async def test_forget_retires_the_named_memories_and_writes_nothing_for_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.handlers as memory_handlers
    from server.memory.app import bulk_set as bulk_set_mod
    from server.memory.app.pipeline import _step_bulk_write
    from server.memory.app.pipeline_state import WorkflowState

    capture = _BulkCapture()
    deleted: list[tuple[str, str, str]] = []

    async def fake_delete(inp):
        deleted.append((inp.memory_key, inp.agent, inp.user_id))

        class _Out:
            tombstoned = True
            version = 1

        return _Out()

    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", capture)
    monkeypatch.setattr(memory_handlers, "handle_memory_delete", fake_delete)
    state = WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        user_id="alice",
        observation_date="2025-11-02",
        extracted_facts=[
            {
                "key": "user_salary",
                "value": "User asked to forget their salary.",
                "forget": True,
                "linked_keys": ["user_salary"],
            },
            {"key": "user_salary", "value": "User earns $120k."},  # same key in the batch → dropped
            {
                "key": "user_coffee_preference",
                "value": "User prefers dark roast.",
                "tags": ["preference"],
            },
        ],
    )
    out = await _step_bulk_write(state, {})
    assert deleted == [("user_salary", "*", "alice")]
    assert out.forgotten == 1 and out.forgotten_keys == ["user_salary"]
    keys = [e.memory_key for e in capture.inputs[0].entries]
    assert keys == ["user_coffee_preference"]


@pytest.mark.asyncio
async def test_forget_with_nothing_else_still_retires(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.handlers as memory_handlers
    from server.memory.app import bulk_set as bulk_set_mod
    from server.memory.app.pipeline import _step_bulk_write
    from server.memory.app.pipeline_state import WorkflowState

    capture = _BulkCapture()
    deleted: list[str] = []

    async def fake_delete(inp):
        deleted.append(inp.memory_key)

        class _Out:
            tombstoned = False  # nothing live under that key any more

        return _Out()

    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", capture)
    monkeypatch.setattr(memory_handlers, "handle_memory_delete", fake_delete)
    state = WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        user_id="alice",
        extracted_facts=[{"key": "user_salary", "forget": True, "linked_keys": ["user_salary"]}],
    )
    out = await _step_bulk_write(state, {})
    assert deleted == ["user_salary"] and out.forgotten == 0 and not capture.inputs


def test_forgotten_reaches_the_output_and_the_ledger() -> None:
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.observability.event_args import project_result

    out = MemorySetFromMessagesOutput(forgotten=2, forgotten_keys=["a", "b"])
    assert project_result(out)["out_forgotten"] == "2"


# ---------------------------------------------------------------- forget guard


def test_is_forget_request_accepts_flag_tag_key_and_sentence() -> None:
    from server.memory.domain.forget_requests import is_forget_request

    assert is_forget_request({"key": "user_salary", "forget": True})
    assert is_forget_request(
        {
            "key": "user_jazz_blues_preference",
            "tags": ["preference", "forget"],
            "value": "User asked to forget their enjoyment of jazz.",
        }
    )
    assert is_forget_request({"key": "user_bible_reading_forget", "value": "x"})
    assert is_forget_request(
        {"key": "k", "value": "User requested to forget their preference for in-person meetings."}
    )
    assert is_forget_request(
        {"key": "k", "value": "User asked the assistant not to remember their salary."}
    )
    assert is_forget_request(
        {"key": "k", "value": "User wants you to stop using their diet in suggestions."}
    )
    assert not is_forget_request(
        {"key": "user_memory_technique", "value": "User forgets names easily and uses mnemonics."}
    )
    assert not is_forget_request(
        {"key": "k", "tags": ["preference"], "value": "User prefers dark roast."}
    )


def test_forget_targets_derives_the_bare_attribute_key() -> None:
    from server.memory.domain.forget_requests import forget_targets

    facts = [
        {
            "key": "user_bible_reading_forget",
            "value": "User asked to forget that they read the Bible daily.",
        }
    ]
    assert forget_targets(facts) == {"user_bible_reading_forget", "user_bible_reading"}
    facts2 = [
        {
            "key": "user_request_forget_in_person_preference",
            "tags": ["forget"],
            "linked_keys": ["user_communication_preference"],
        }
    ]
    assert forget_targets(facts2) == {
        "user_request_forget_in_person_preference",
        "user_request_in_person_preference",
        "user_communication_preference",
    }


@pytest.mark.asyncio
async def test_a_phrased_forget_request_is_never_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.handlers as memory_handlers
    from server.memory.app import bulk_set as bulk_set_mod
    from server.memory.app.pipeline import _step_bulk_write
    from server.memory.app.pipeline_state import WorkflowState

    capture = _BulkCapture()
    deleted: list[str] = []

    async def fake_delete(inp):
        deleted.append(inp.memory_key)

        class _Out:
            tombstoned = inp.memory_key == "user_bible_reading"

        return _Out()

    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", capture)
    monkeypatch.setattr(memory_handlers, "handle_memory_delete", fake_delete)
    state = WorkflowState(
        org="dev",
        namespace="ns",
        agent="a",
        user_id="alice",
        extracted_facts=[
            {
                "key": "user_bible_reading_forget",
                "tags": ["preference", "forget"],
                "value": "User asked to forget that they read the Bible daily.",
            },
            {
                "key": "user_drink_preference",
                "tags": ["preference"],
                "value": "User enjoys fresh fruit juices.",
            },
        ],
    )
    out = await _step_bulk_write(state, {})
    assert (
        sorted(deleted) == ["user_bible_reading", "user_bible_reading_forget"]
        and out.forgotten == 1
    )
    assert [e.memory_key for e in capture.inputs[0].entries] == ["user_drink_preference"]


# ---------------------------------------------------------------- forget by content


def _by_content(fact: dict[str, Any], rows: list[dict[str, Any]]) -> set[str]:
    """The rows a forget entry names by content: its targets less its own keys."""
    from server.memory.domain.forget_requests import forget_targets

    fact = {**fact, "forget": True}
    return forget_targets([fact], rows) - forget_targets([fact], [])


def test_a_forget_request_by_content_finds_the_row_the_request_names() -> None:
    from server.memory.domain.forget_requests import forget_targets

    existing = [
        {
            "memory_key": "user_music_preference",
            "value": "User enjoys jazz and blues performances.",
        },
        {"memory_key": "user_drink_preference", "value": "User enjoys fresh fruit juices."},
        {"memory_key": "user_commute_mode", "value": "User has started cycling to work."},
    ]
    fact = {
        "key": "user_jazz_blues_forget",
        "tags": ["preference", "forget"],
        "value": "User asked to forget their enjoyment of jazz and blues performances.",
    }
    assert _by_content(fact, existing) == {"user_music_preference"}
    # the batch-level helper falls back to content only when nothing was linked
    assert forget_targets([fact], existing) == {
        "user_jazz_blues_forget",
        "user_jazz_blues",
        "user_music_preference",
    }
    linked = dict(fact, linked_keys=["user_music_preference"])
    assert forget_targets([linked], existing) == {
        "user_jazz_blues_forget",
        "user_jazz_blues",
        "user_music_preference",
    }


def test_a_forget_request_by_content_needs_real_overlap() -> None:
    existing = [
        {"memory_key": "user_bible_reading", "value": "User reads the Bible every morning."}
    ]
    assert (
        _by_content(
            {"key": "user_salary_forget", "value": "User asked to forget their salary."}, existing
        )
        == set()
    )
    # one shared word is not a target
    assert (
        _by_content(
            {"key": "k", "value": "User asked to forget their morning coffee order."}, existing
        )
        == set()
    )
    assert _by_content(
        {"key": "k", "value": "User asked you not to remember that they read the Bible daily."},
        existing,
    ) == {"user_bible_reading"}


# --- a request to forget is read from the user's own message ---


def _u(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


def test_forget_request_is_read_from_the_users_sentence() -> None:
    from server.memory.domain.forget_requests import forget_requests_from_messages

    got = forget_requests_from_messages(_u("Please forget that I like scenic train journeys."))
    assert [g["value"] for g in got] == ["I like scenic train journeys"]
    assert got[0]["forget"] is True and got[0]["key"] == ""


@pytest.mark.parametrize(
    "sentence, clause",
    [
        ("Could you please forget what I told you about my salary earlier?", "my salary earlier"),
        (
            "Please don't remember that I have presbyopia and need reading glasses.",
            "I have presbyopia and need reading glasses",
        ),
        ("Delete the note about my ex-boyfriend from Boston.", "my ex-boyfriend from Boston"),
        ("I'd like you to erase what you know about my therapy sessions.", "my therapy sessions"),
        (
            "Disregard the fact that I said I keep a personal journal.",
            "I said I keep a personal journal",
        ),
        ("Never mind what I said about my divorce lawyer.", "my divorce lawyer"),
        (
            "Thanks! By the way, please forget that I told you I collect vintage cameras.",
            "I collect vintage cameras",
        ),
    ],
)
def test_forget_request_shapes_yield_the_named_clause(sentence: str, clause: str) -> None:
    from server.memory.domain.forget_requests import forget_requests_from_messages

    got = forget_requests_from_messages(_u(sentence))
    assert [g["value"] for g in got] == [clause]


@pytest.mark.parametrize(
    "sentence",
    [
        "Don't forget to remind me about the dentist.",
        "I always forget my keys.",
        "Stop using so many emojis in your replies.",
        "Remove the skin before cooking the chicken.",
        "Never mind, let's move on.",
        "Please remember that I like jazz.",
        "Can you delete the second paragraph of the email?",
        "Forget it, let's talk about something else.",
        "Forget about it. Anyway, how do I bake bread?",
        "Drop the formal tone and be casual.",
        "Please forget everything and start over.",
    ],
)
def test_a_sentence_that_is_not_a_request_to_forget_yields_nothing(sentence: str) -> None:
    from server.memory.domain.forget_requests import forget_requests_from_messages

    assert forget_requests_from_messages(_u(sentence)) == []


def test_only_the_users_sentences_are_read() -> None:
    from server.memory.domain.forget_requests import forget_requests_from_messages

    msgs = [
        {"role": "assistant", "content": "Please forget that I like scenic train journeys."},
        {"role": "system", "content": "Forget that the user has presbyopia."},
    ]
    assert forget_requests_from_messages(msgs) == []


def test_forget_requests_from_messages_resolve_against_existing_rows() -> None:
    from server.memory.domain.forget_requests import forget_requests_from_messages, forget_targets

    existing = [
        {"memory_key": "user_scenic_train_journeys", "value": "User enjoys scenic train journeys."},
        {
            "memory_key": "user_presbyopia",
            "value": "User has presbyopia and needs reading glasses.",
        },
        {"memory_key": "user_journal", "value": "User keeps a personal journal."},
        {
            "memory_key": "user_travel_style",
            "value": "User prefers train travel for work trips across Europe.",
        },
        {"memory_key": "user_salary", "value": "User earns 92k a year."},
        {
            "memory_key": "friend_salary_talk",
            "value": "User's friend Bob complained about his salary.",
        },
    ]
    msgs = _u(
        "Please forget that I like scenic train journeys. "
        "Also, don't remember that I keep a personal journal. "
        "And forget what I told you about my salary earlier."
    )
    requests = forget_requests_from_messages(msgs)
    assert len(requests) == 3
    assert forget_targets(requests, existing) == {
        "user_scenic_train_journeys",
        "user_journal",
        "user_salary",
    }


def test_one_word_forget_request_only_resolves_against_the_users_rows() -> None:
    fact = {"key": "", "value": "my salary", "forget": True}
    rows = [
        {
            "memory_key": "friend_salary_talk",
            "value": "User's friend Bob complained about his salary.",
        },
        {"memory_key": "user_salary", "value": "User earns 92k a year."},
    ]
    assert _by_content(fact, rows) == {"user_salary"}


# --- whose fact a sentence states is read from its subject ---


@pytest.mark.parametrize(
    "value, key, expected",
    [
        (
            "User's friend Mark feels public conversations are less about evidence.",
            "user_mark_public_conversations",
            "other",
        ),
        (
            "User's colleague Elaine expressed interest in collaborating.",
            "user_colleague_email_content",
            "other",
        ),
        ("User's partner works in a studio.", "user_partner", "other"),
        ("User's aunt has been feeling low since moving.", "user_aunt_low_mood", "other"),
        ("User's name is Mateo.", "user_name", "user"),
        ("User's doctor reminded them to catch sunlight.", "user_doctor_advice", None),
        ("User's teacher said the tiger drawing looked great.", "user_teacher_feedback", None),
        ("User's parents moved back to India permanently.", "user_parents_location", "other"),
        ("User's sister is named Hazel Monroe.", "user_sister_name", "user"),
        ("User's cousin is called Finn Callahan.", "user_cousin_name", "user"),
        (
            "Father Dominic asked what happens when a planet is in retrograde.",
            "father_dominic_planet_retrograde",
            "other",
        ),
        (
            "User's note in Konkani translates to a memory of village service.",
            "user_konkani_note_translation",
            "user",
        ),
        ("User enjoys podcasts about world history.", "user_podcast_taste", "user"),
        (
            "Kenji has difficulty sleeping because of unfinished writing projects.",
            "kenji_sleep",
            "other",
        ),
        (
            "Dr. Nakamura is interested in blood pressure variability in older adults.",
            "dr_nakamura_blood_pressure",
            "other",
        ),
        (
            "Father Dominic asked for an explanation of e-ink tablets.",
            "father_dominic_eink_tablets",
            "other",
        ),
        (
            "Elliot is advising a younger coach with low confidence.",
            "elliot_advising_younger_coach",
            "other",
        ),
        (
            "Chris observed that low-profile camping gear holds up well.",
            "chris_observation_camping_gear",
            "other",
        ),
        (
            "Assistant recommended the podcast Throughline.",
            "assistant_recommended_throughline",
            "assistant",
        ),
        (
            "Gallstones are solid deposits that form in the gallbladder.",
            "gallstones_definition",
            None,
        ),
        ("Cars are less efficient on short trips.", "car_efficiency_short_trips", None),
        (
            "Abstract Expressionism is an art movement focused on raw emotion.",
            "abstract_expressionism_definition",
            None,
        ),
        ("Morning light exposure sharpens cognitive function.", "morning_light_effect", None),
        (
            "Elaine invited User to set up a call next week.",
            "user_colleague_email_invitation",
            "user",
        ),
    ],
)
def test_ownership_is_read_from_the_subject(value: str, key: str, expected: str | None) -> None:
    from server.memory.domain.ownership import ownership_of

    assert ownership_of({"key": key, "value": value}) == expected


def test_the_users_own_name_as_subject_is_still_the_user() -> None:
    from server.memory.domain.ownership import ownership_of, user_names_in

    existing = [{"memory_key": "user_name", "value": "User's name is Mateo."}]
    names = user_names_in([], existing)
    assert names == {"mateo"}
    priest = user_names_in(
        [], [{"memory_key": "user_name", "value": "User's name is Fr. Dominic Rajesh Pereira."}]
    )
    assert priest == {"dominic", "rajesh", "pereira"}
    q = {
        "key": "father_dominic_planet_retrograde",
        "value": "Father Dominic asked what happens when a planet is in retrograde.",
    }
    assert ownership_of(q, user_names=priest) == "user"
    fact = {"key": "mateo_speech_class", "value": "Mateo attends speech class twice a week."}
    assert ownership_of(fact, user_names=names) == "user"
    assert ownership_of(fact) == "other"


def test_forget_request_drops_the_fact_extracted_from_the_same_batch() -> None:
    from server.memory.domain.forget_requests import forget_requests_from_messages, forget_targets

    msgs = [
        {"role": "user", "content": "Can you suggest some audio series for my commute?"},
        {
            "role": "assistant",
            "content": "Since you enjoy podcasts about world history, try Hardcore History.",
        },
        {"role": "user", "content": "Please forget that I enjoy podcasts about world history."},
        {"role": "assistant", "content": "Got it."},
    ]
    facts = [
        {"key": "user_podcast_taste", "value": "User enjoys podcasts about world history."},
        {
            "key": "assistant_recommended_hardcore_history",
            "value": "Assistant recommended Hardcore History.",
        },
    ]
    requests = forget_requests_from_messages(msgs)
    assert forget_targets(facts + requests, existing=[]) == {"user_podcast_taste"}


# --- a premise the assistant states about the user is the user's fact ---


def _a(text: str, idx: int = 1) -> list[dict[str, str]]:
    msgs = [{"role": "user", "content": "Can you help me with something?"}]
    msgs.insert(idx, {"role": "assistant", "content": text})
    return msgs


@pytest.mark.parametrize(
    "sentence, value, tags",
    [
        (
            "Since you enjoy podcasts about world history, you might like 'Hardcore History'.",
            "User enjoys podcasts about world history.",
            ["preference", "preference:podcast", "premise"],
        ),
        (
            "Given that you have presbyopia and need reading glasses, try a clip-on lamp.",
            "User has presbyopia and need reading glasses.",
            ["premise"],
        ),
        (
            "Since you already keep a personal journal, you could end each entry with a theme.",
            "User already keeps a personal journal.",
            ["premise"],
        ),
        (
            "As someone who prefers to avoid mainstream reality TV, you might enjoy documentaries.",
            "User prefers to avoid mainstream reality TV.",
            ["preference", "preference:tv", "premise"],
        ),
        (
            "Since you're monitoring a vitamin D deficiency with annual blood tests, keep a log.",
            "User is monitoring a vitamin D deficiency with annual blood tests.",
            ["premise"],
        ),
        (
            "Because you love trying new dessert recipes, here are three to start with.",
            "User loves trying new dessert recipes.",
            ["preference", "preference:recipe", "premise"],
        ),
        (
            "Since you already practice light stretching in the morning, add two poses.",
            "User already practices light stretching in the morning.",
            ["preference", "preference:stretching", "premise"],
        ),
    ],
)
def test_premise_about_the_user_becomes_the_users_fact(
    sentence: str, value: str, tags: list[str]
) -> None:
    from server.memory.domain.premise_facts import premise_facts_from_messages

    got = premise_facts_from_messages(_a(sentence), [], [])
    assert [g["value"] for g in got] == [value]
    assert got[0]["tags"] == tags
    assert got[0]["key"].startswith("user_") and got[0]["about"] == "user"
    assert got[0]["source_message_indices"] == [1]


@pytest.mark.parametrize(
    "sentence",
    [
        "Since you still want inspiration about new places and cultures, here are a few.",
        "Since you're after something cozy and satisfying, try a stew.",
        "Since you asked for a formal tone, I kept the greeting brief.",
        "Since you mentioned the email should be shorter, I cut the second paragraph.",
        "Given that you are writing this note for your manager, keep it direct.",
        "If you enjoy scenic train journeys, you might love the Glacier Express.",
        "Since it is raining, take an umbrella.",
    ],
)
def test_a_premise_about_the_task_or_a_guess_is_not_a_fact(sentence: str) -> None:
    from server.memory.domain.premise_facts import premise_facts_from_messages

    assert premise_facts_from_messages(_a(sentence), [], []) == []


def test_premise_the_extractor_already_wrote_is_not_written_twice() -> None:
    from server.memory.domain.premise_facts import premise_facts_from_messages

    msgs = _a("Since you enjoy podcasts about world history, try Throughline.")
    facts = [{"key": "user_podcast_taste", "value": "User enjoys podcasts about world history."}]
    assert premise_facts_from_messages(msgs, facts, []) == []
    existing = [
        {"memory_key": "user_history_podcasts", "value": "User likes world history podcasts."}
    ]
    assert premise_facts_from_messages(msgs, [], existing) == []


def test_only_assistant_turns_carry_a_premise() -> None:
    from server.memory.domain.premise_facts import premise_facts_from_messages

    msgs = [
        {"role": "user", "content": "Since you enjoy podcasts about world history, recommend one."}
    ]
    assert premise_facts_from_messages(msgs, [], []) == []


def test_premise_then_forget_in_one_window_writes_nothing() -> None:
    from server.memory.domain.forget_requests import forget_requests_from_messages, forget_targets
    from server.memory.domain.premise_facts import premise_facts_from_messages

    msgs = [
        {"role": "user", "content": "Can you suggest some audio series for my commute?"},
        {
            "role": "assistant",
            "content": "Since you enjoy podcasts about world history, try Hardcore History.",
        },
        {"role": "user", "content": "Please forget that I enjoy podcasts about world history."},
        {"role": "assistant", "content": "Got it."},
    ]
    premise = premise_facts_from_messages(msgs, [], [])
    assert len(premise) == 1
    requests = forget_requests_from_messages(msgs)
    assert forget_targets(premise + requests, existing=[]) == {premise[0]["key"]}


def test_third_person_conjugation() -> None:
    from server.memory.domain.premise_facts import _third_person

    assert [
        _third_person(v) for v in ("enjoy", "have", "study", "watch", "play", "do", "go", "prize")
    ] == [
        "enjoys",
        "has",
        "studies",
        "watches",
        "plays",
        "does",
        "goes",
        "prizes",
    ]


def test_the_assistants_recommendation_does_not_suppress_the_premise() -> None:
    from server.memory.domain.premise_facts import premise_facts_from_messages

    msgs = [
        {"role": "user", "content": "Can you suggest some engaging audio series for my commute?"},
        {
            "role": "assistant",
            "content": "Since you enjoy podcasts about world history, you might like Hardcore History.",
        },
    ]
    facts = [
        {
            "key": "assistant_recommended_audio_series",
            "value": "Assistant recommended audio series about world history podcasts: Hardcore History.",
        }
    ]
    got = premise_facts_from_messages(msgs, facts, [])
    assert [g["value"] for g in got] == ["User enjoys podcasts about world history."]
    # but the user's own fact does
    facts.append(
        {"key": "user_podcast_taste", "value": "User enjoys podcasts about world history."}
    )
    assert premise_facts_from_messages(msgs, facts, []) == []


@pytest.mark.asyncio
async def test_forget_request_fetches_the_rows_nearest_its_own_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import pipeline as mp
    from server.memory.app import pipeline_state as pipeline_state
    from server.memory.domain import forget_requests as forget

    calls: list[str] = []

    async def fake_fetch(identity, messages, limit=50):
        calls.append(messages[0]["content"])
        return [{"memory_key": "user_journal", "value": "User keeps a personal journal."}]

    import server.memory.app.handlers as hm

    monkeypatch.setattr(hm, "_fetch_relevant_existing_memories", fake_fetch)
    state = pipeline_state.WorkflowState(
        org="o", namespace="n", agent="a", user_id="u", session_id="s", messages=[]
    )
    state.existing_memories = [{"memory_key": "user_other", "value": "User likes tea."}]
    rows = await mp._fetch_for_forget_requests(
        state, [{"key": "", "value": "I keep a personal journal", "forget": True}]
    )
    assert calls == ["I keep a personal journal"]
    assert [r["memory_key"] for r in rows] == ["user_journal"]
    assert forget.forget_targets(
        [{"key": "", "value": "I keep a personal journal", "forget": True}],
        state.existing_memories + rows,
    ) == {"user_journal"}


@pytest.mark.asyncio
async def test_existing_memories_resolve_across_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rows the vector hits name are read with the session
    axis open — keyed to the session being written they would come back empty
    for every row an earlier session wrote."""
    from types import SimpleNamespace

    import server.memory.app.handlers as hm
    from server.memory import store as memory_repo

    class _Hit:
        def __init__(self, k: str) -> None:
            self.memory_key = k

    class _Emb:
        model_name = "fake"

        async def embed(self, text: str) -> list[float]:
            return [0.0]

    seen: dict[str, object] = {}

    async def fake_vector_search(q):
        return [_Hit("user_journal")]

    async def fake_get_many(identity, keys, **kw):
        seen["identity"] = identity
        seen["keys"] = list(keys)
        seen["exclude_high"] = kw.get("exclude_high_sensitivity")
        return [
            SimpleNamespace(
                memory_key="user_journal", value="User keeps a journal.", sensitivity=""
            )
        ]

    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Emb())
    monkeypatch.setattr("server.memory.app.retrieval.vector_search", fake_vector_search)
    monkeypatch.setattr(memory_repo, "get_many_by_keys", fake_get_many)
    ident = memory_repo.Identity(org="o", namespace="n", agent="a", user_id="u", session_id="ns:s4")
    rows = await hm._fetch_relevant_existing_memories(
        ident, [{"role": "user", "content": "I keep a personal journal"}], limit=10
    )
    assert [r["memory_key"] for r in rows] == ["user_journal"]
    assert seen["identity"].session_id == "*"
    assert seen["identity"].user_id == "u" and seen["identity"].agent == "a"
    assert seen["exclude_high"] is True


@pytest.mark.asyncio
async def test_the_extraction_prompt_never_shows_a_high_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-session lookup leaves 'high' rows out before it picks one per key, so a
    'high' row never reaches the existing memories the extraction prompt sends the LLM and
    another session's 'high' row of a key hides no plain one."""
    from types import SimpleNamespace

    import server.memory.app.handlers as hm
    from server.memory import store as memory_repo

    class _Emb:
        model_name = "fake"

        async def embed(self, text: str) -> list[float]:
            return [0.0]

    async def fake_vector_search(q):
        return [
            SimpleNamespace(memory_key="user_journal"),
            SimpleNamespace(memory_key="vault"),
            SimpleNamespace(memory_key="copied"),
        ]

    rows = {
        "user_journal": SimpleNamespace(
            memory_key="user_journal", value="User keeps a journal.", sensitivity=""
        ),
        "vault": SimpleNamespace(
            memory_key="vault", value="the door code is 4412", sensitivity="high"
        ),
        # copied in unlabelled (copy_to, an import) before the sweep ran
        "copied": SimpleNamespace(
            memory_key="copied", value="deploy token:\nghp_" + "a" * 36, sensitivity=""
        ),
    }

    async def fake_get_many(identity, keys, *, scope, exclude_high_sensitivity):
        return [
            r for r in rows.values() if not (exclude_high_sensitivity and r.sensitivity == "high")
        ]

    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Emb())
    monkeypatch.setattr("server.memory.app.retrieval.vector_search", fake_vector_search)
    monkeypatch.setattr(memory_repo, "get_many_by_keys", fake_get_many)
    ident = memory_repo.Identity(org="o", namespace="n", agent="a", user_id="u", session_id="s")
    rows = await hm._fetch_relevant_existing_memories(
        ident, [{"role": "user", "content": "what is the door code"}], limit=10
    )
    assert [r["memory_key"] for r in rows] == ["user_journal"]
