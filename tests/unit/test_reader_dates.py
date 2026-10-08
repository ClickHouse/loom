"""Read-time grounding of bare relative phrases in fact excerpts."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.domain.reader_dates import ground_excerpt, ground_hits


def test_past_closed_class_phrase_gets_the_row_date() -> None:
    assert ground_excerpt("Calvin attended a fancy gala in Boston yesterday.", "2023-11-16") == (
        "Calvin attended a fancy gala in Boston yesterday (on 2023-11-16)."
    )
    assert ground_excerpt(
        "Evan lost his job last month due to downsizing.", "2023-10-09T00:00:00"
    ) == ("Evan lost his job last month due to downsizing (around 2023-10-09).")
    assert ground_excerpt('"Dave had a jam session with his band last night."', "2023-09-14") == (
        '"Dave had a jam session with his band last night (on 2023-09-14)."'
    )


def test_untouched_when_grounded_vague_future_or_no_phrase() -> None:
    assert ground_excerpt(
        "John had a career-high on 2023-12-08 (last Friday).", "2023-12-08"
    ).endswith("(last Friday).")
    assert ground_excerpt("Caroline moved from Sweden a while ago.", "2023-06-01").endswith(
        "a while ago."
    )
    assert ground_excerpt(
        "Sam and Evan plan a painting session next Saturday.", "2023-09-11"
    ).endswith("next Saturday.")
    assert ground_excerpt("We might do it tomorrow.", "2023-09-11") == "We might do it tomorrow."
    assert (
        ground_excerpt("Jon started his own business.", "2023-03-16")
        == "Jon started his own business."
    )
    assert ground_excerpt("yesterday", "") == "yesterday" and ground_excerpt("", "2023-01-01") == ""
    assert ground_excerpt("She left yesterday.", "not-a-date") == "She left yesterday."


def _hit(
    key: str, source: str, text: str, valid_at: str = "2023-11-16", anchor: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key,
        source=source,
        content_excerpt=text,
        valid_at=valid_at,
        temporal_anchor=anchor,
    )


def test_ground_hits_skips_verbatim_planes_and_prefers_the_anchor() -> None:
    hits = [
        _hit("user_calvin_gala", "fact", "Calvin attended a gala yesterday."),
        _hit(
            "chunk:ns:session_30:1",
            "chunk",
            "[user] Calvin: I went to a gala yesterday!",
            "2023-11-17",
        ),
        _hit(
            "summary:ns:session_30",
            "summary",
            "On 2023-11-17 Calvin said he went to a gala yesterday.",
            "2023-11-17",
        ),
        _hit(
            "dave_jam",
            "fact",
            "Dave jammed with his band last night.",
            "2023-09-15",
            anchor="2023-09-14",
        ),
        {
            "memory_key": "d",
            "source": "fact",
            "content_excerpt": "Nate won last week.",
            "valid_at": "2022-01-14",
        },
    ]
    assert ground_hits(hits) == 3
    assert hits[0].content_excerpt.endswith("yesterday (on 2023-11-16).")
    assert (
        hits[1].content_excerpt.endswith("yesterday!")
        and "2023" not in hits[1].content_excerpt[-14:]
    )
    assert hits[2].content_excerpt.endswith("yesterday.")
    assert hits[3].content_excerpt.endswith("(on 2023-09-14).")  # temporal_anchor beats valid_at
    assert hits[4]["content_excerpt"].endswith("(around 2022-01-14).")


def test_request_field_defaults_on() -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.knobs import _ground_relative_excerpts_enabled

    assert MemorySearchInput.model_fields["ground_relative_dates"].default is None
    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    assert _ground_relative_excerpts_enabled(inp) is True
    assert (
        _ground_relative_excerpts_enabled(inp.model_copy(update={"ground_relative_dates": False}))
        is False
    )
    from server.memory.app.ask.prompts import _SYNTH_SYSTEM

    assert "ALREADY resolved" in _SYNTH_SYSTEM and "date it was recorded" not in _SYNTH_SYSTEM
