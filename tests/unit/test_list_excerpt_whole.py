"""An enumeration is one unit when a chunk excerpt is trimmed."""

from __future__ import annotations

from server.memory.domain.passage_trim import is_enumeration, trim_chunk_excerpt

LIST_TURN = (
    "[assistant] I apologize if my previous suggestions were not helpful. Here are some other "
    "alternatives:\n\n"
    "1. Sexual fixations - This term implies a strong preoccupation with sexual thoughts or "
    "behaviors that may interfere with other areas of life.\n"
    "2. Problematic sexual behaviors - This phrase is straightforward and describes behaviors "
    "that cause problems or difficulties in an individual's life.\n"
    "3. Sexual impulsivity - This term emphasizes the impulsive or compulsive nature of certain "
    "sexual behaviors and the difficulties individuals may face in controlling them.\n"
    "4. Compulsive sexuality - This phrase emphasizes the compulsive aspect of the behaviors."
)
QUERY = (
    "In our previous chat, you suggested 'sexual compulsions' and a few other options for "
    "alternative terms for certain behaviors. Can you remind me what the other four options were?"
)


def test_a_numbered_list_is_recognised() -> None:
    assert is_enumeration(LIST_TURN)
    assert not is_enumeration("[assistant] One option is X. Another is Y. A third is Z.")


def test_the_list_survives_the_trim_whole() -> None:
    out = trim_chunk_excerpt(LIST_TURN, QUERY)
    for item in (
        "Sexual fixations",
        "Problematic sexual behaviors",
        "Sexual impulsivity",
        "Compulsive sexuality",
    ):
        assert item in out


def test_a_prose_turn_is_still_windowed() -> None:
    prose = (
        "[assistant] "
        + " ".join(
            f"Sentence number {i} talks about something unrelated to the question at hand."
            for i in range(12)
        )
        + " The answer about compulsive behaviors and alternative terms sits here."
    )
    out = trim_chunk_excerpt(prose, QUERY)
    assert len(out) < len(prose)
    assert "alternative terms" in out


def test_a_very_long_list_falls_back_to_the_window() -> None:
    long_list = "[assistant] Options:\n" + "\n".join(
        f"{i}. Option {i} described at some length with many words to pad the line out further."
        for i in range(1, 40)
    )
    out = trim_chunk_excerpt(long_list, "what were the options")
    assert len(out) < len(long_list)
