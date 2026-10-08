"""An ordinal in the question names a list line the store holds verbatim."""

from __future__ import annotations

from server.memory.app.ask.identifiers import _injectable_identifiers, ordinal_line_markers


def test_numeric_and_word_ordinals_become_line_markers() -> None:
    q = "I remember you provided a list of 100 prompt parameters. Can you remind me what the 27th parameter was?"
    assert ordinal_line_markers(q) == ["\n27."]
    assert ordinal_line_markers("What was the fourth option you listed?") == ["\n4."]


def test_a_bare_first_or_a_date_is_not_a_marker() -> None:
    assert ordinal_line_markers("Who did I meet first, Mark or Tom?") == []
    assert ordinal_line_markers("On the 15th of March I bought a necklace.") == [
        "\n15."
    ]  # harmless: no list line
    assert ordinal_line_markers("") == []


def test_markers_ride_the_identifier_injection() -> None:
    q = "Can you remind me what the 27th parameter was?"
    assert "\n27." in _injectable_identifiers(q)
