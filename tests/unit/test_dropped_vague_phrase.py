"""A vague past offset the extractor dropped comes back as a note."""

from __future__ import annotations

from server.memory.domain.extraction_temporal import (
    dropped_phrase_sentence,
    dropped_vague_phrase_sentence,
)

CITED = (
    "I'm planning to volunteer at another charity event this weekend and I was wondering "
    "if you could help me come up with some conversation starters. I recall that a few "
    "months ago, I volunteered at a charity event and met some like-minded people, "
    "including a guy named Tom who is involved with the organization."
)


def test_vague_phrase_dropped_from_the_value_is_recovered() -> None:
    value = (
        "User previously volunteered at a charity event where they met like-minded people, "
        "including a guy named Tom who is involved with the organization."
    )
    hit = dropped_vague_phrase_sentence(value, CITED)
    assert hit is not None
    assert hit[1].lower() == "a few months ago"


def test_present_or_future_sentence_is_not_a_candidate() -> None:
    value = (
        "User is planning to volunteer at another charity event and wants conversation starters."
    )
    assert dropped_vague_phrase_sentence(value, CITED) is None


def test_value_that_keeps_its_own_phrase_or_date_is_left_alone() -> None:
    assert dropped_vague_phrase_sentence("User met Tom a few months ago.", CITED) is None
    assert dropped_vague_phrase_sentence("User met Tom on 2023-02-10.", CITED) is None


def test_concrete_phrase_is_a_dropped_phrase_not_a_vague_one() -> None:
    cited = "Last week I finally shared my book with my writers group and they loved it."
    value = "User shared their book with their writers group and received great feedback."
    assert dropped_phrase_sentence(value, cited) is not None
    assert dropped_vague_phrase_sentence(value, cited) is None


def test_pipeline_appends_the_note_without_moving_the_anchor() -> None:
    from server.memory.app.extract import ExtractedFact
    from server.memory.app.extract.dates import _recover_dropped

    f = ExtractedFact(
        memory_key="user_volunteer_event",
        value="User previously volunteered at a charity event where they met like-minded "
        "people, including a guy named Tom who is involved with the organization.",
    )
    out = _recover_dropped(f, CITED, "2023-05-28")
    assert out is not None
    assert out.value.endswith("(a few months ago as of 2023-05-28).")
    assert out.temporal_anchor == f.temporal_anchor
    assert out.valid_at == f.valid_at


def test_general_statement_does_not_borrow_a_phrase() -> None:
    value = "User appreciates community organizations."
    assert dropped_vague_phrase_sentence(value, CITED) is None


def test_session_day_written_as_the_event_date_is_replaced_by_the_note() -> None:
    from server.memory.app.extract import ExtractedFact
    from server.memory.app.extract.dates import _recover_dropped

    cited = (
        "That's great advice, thanks! I've also been thinking about my car's engine "
        "performance, I noticed a slight misfire during my daily commute a while back, but "
        "after replacing the spark plugs with new ones from NGK, the engine felt much smoother."
    )
    f = ExtractedFact(
        memory_key="user_spark_plug_replacement",
        value="User replaced spark plugs with new ones from NGK on 2023-03-15 after noticing a "
        "slight misfire during their daily commute, which improved engine performance.",
    )
    out = _recover_dropped(f, cited, "2023-03-15")
    assert out is not None
    assert "on 2023-03-15" not in out.value
    assert out.value.endswith("(a while back as of 2023-03-15).")
    assert out.value.startswith("User replaced spark plugs with new ones from NGK after noticing")


def test_a_different_iso_date_in_the_value_is_left_alone() -> None:
    cited = "I noticed a misfire a while back, but after replacing the spark plugs it runs well."
    value = "User replaced the spark plugs on 2023-02-14 after noticing a misfire."
    assert dropped_vague_phrase_sentence(value, cited, said_on="2023-03-15") is None
