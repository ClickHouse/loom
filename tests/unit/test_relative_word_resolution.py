"""A relative day phrase is read as the event date the row carries."""

from server.memory.domain.extraction_temporal import resolve_relative_phrase


def test_point_phrase_is_rewritten_into_the_event_date() -> None:
    assert (
        resolve_relative_phrase(
            "Melanie made a plate in pottery class yesterday and finds pottery relaxing.",
            "2023-08-24 00:00:00",
        )
        == "Melanie made a plate in pottery class on 2023-08-24 and finds pottery relaxing."
    )
    assert (
        resolve_relative_phrase("Evan got drunk last night with friends.", "2023-01-09")
        == "Evan got drunk on 2023-01-09 with friends."
    )


def test_span_phrase_becomes_around_and_the_note_is_dropped_when_it_repeats_the_date() -> None:
    assert (
        resolve_relative_phrase("Maria joined a gym last week (around 2023-06-09).", "2023-06-09")
        == "Maria joined a gym around 2023-06-09."
    )
    # a note with a DIFFERENT date is kept: it carries information the anchor lacks
    assert (
        resolve_relative_phrase("Maria joined a gym last week (around 2023-06-02).", "2023-06-09")
        == "Maria joined a gym around 2023-06-09 (around 2023-06-02)."
    )


def test_span_phrase_reads_against_the_session_date_when_the_row_carries_one() -> None:
    # "last week" said on 2023-06-09 is "the week before
    # 2023-06-09" — not "around 2023-06-02", which a reader takes one more
    # step back from. The write-time note is dropped when it says the same.
    assert (
        resolve_relative_phrase(
            "Maria joined a gym last week (the week before 2023-06-09).",
            "2023-06-02",
            "2023-06-09T00:00:00",
        )
        == "Maria joined a gym the week before 2023-06-09."
    )
    assert (
        resolve_relative_phrase(
            "Maria joined a gym last week (around 2023-06-02).", "2023-06-02", "2023-06-09"
        )
        == "Maria joined a gym the week before 2023-06-09."
    )
    assert (
        resolve_relative_phrase("Maria ran a 5k two weeks ago.", "2023-05-26", "2023-06-09")
        == "Maria ran a 5k about two weeks before 2023-06-09."
    )
    # No session date → the anchor-relative wording.
    assert (
        resolve_relative_phrase("Maria joined a gym last week.", "2023-06-02")
        == "Maria joined a gym around 2023-06-02."
    )


def test_vague_phrases_missing_anchors_and_plain_text_are_untouched() -> None:
    assert (
        resolve_relative_phrase("They met a few years ago.", "2020-01-23")
        == "They met a few years ago."
    )
    assert (
        resolve_relative_phrase("Melanie made a plate yesterday.", "")
        == "Melanie made a plate yesterday."
    )
    assert (
        resolve_relative_phrase("Melanie made a plate yesterday.", "not-a-date")
        == "Melanie made a plate yesterday."
    )
    assert (
        resolve_relative_phrase("Melanie made a plate on 2023-08-24.", "2023-08-24")
        == "Melanie made a plate on 2023-08-24."
    )


def test_handler_step_rewrites_anchored_facts_only() -> None:
    from types import SimpleNamespace

    from server.memory.app.search.payload import _resolve_relative_words

    fact = SimpleNamespace(
        source="fact",
        temporal_anchor="2023-08-24",
        valid_at="2023-08-25",
        content_excerpt="Melanie made a plate yesterday.",
    )
    summary = SimpleNamespace(
        source="summary",
        temporal_anchor="",
        valid_at="2023-08-25",
        content_excerpt="On 2023-08-25, Melanie said she made a plate yesterday.",
    )
    chunk = SimpleNamespace(
        source="chunk",
        temporal_anchor="2023-08-24",
        valid_at="",
        content_excerpt="Melanie: Yeah, I made it in pottery class yesterday.",
    )
    unanchored = SimpleNamespace(
        source="fact",
        temporal_anchor="",
        valid_at="2023-08-25",
        content_excerpt="Melanie made a plate yesterday.",
    )
    _resolve_relative_words([fact, summary, chunk, unanchored])
    assert (
        fact.content_excerpt == "Melanie made a plate on 2023-08-24."
    )  # the EVENT date, not the session
    # a summary's only date is the session's; "yesterday" against it would be a day late
    assert summary.content_excerpt == "On 2023-08-25, Melanie said she made a plate yesterday."
    assert (
        chunk.content_excerpt == "Melanie: Yeah, I made it in pottery class yesterday."
    )  # verbatim stays
    assert (
        unanchored.content_excerpt == "Melanie made a plate yesterday."
    )  # no event anchor, no rewrite
