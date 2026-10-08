"""A comparison is decided by the two values the answer states."""

from __future__ import annotations

from server.memory.domain.ask_compare import comparative_of, value_near, verify_comparison


def test_comparatives_and_values() -> None:
    assert comparative_of("Who is older, Dylan Carter or Savannah Reed?") == (
        "older",
        "number",
        True,
    )
    assert comparative_of("Whose birthday comes later in the year, A or B?") == (
        "later",
        "date",
        True,
    )
    assert comparative_of("Which event, TasteFest or LaunchPad, has a longer duration?") == (
        "longer",
        "duration",
        True,
    )
    assert comparative_of("What is my sister's name?") is None
    a = "B) Savannah Reed — Dylan Carter is 40 [chunk:s2:4]; Savannah Reed is 31 [chunk:s2:10]"
    assert value_near(a, "Dylan Carter", "number", ("Savannah Reed",)) == 40
    assert value_near(a, "Savannah Reed", "number", ("Dylan Carter",)) == 31
    assert value_near("Jaxon Hale's birthday 2024-12-09", "Jaxon Hale", "date") == 1209
    assert value_near("LaunchPad lasts nine days", "LaunchPad", "duration") == 9


def test_older_later_longer_and_ties_are_recomputed() -> None:
    q = "Who is older, Dylan Carter or Savannah Reed?\nA) Dylan Carter\nB) Savannah Reed\nC) Both the same\nD) Both incorrect"
    r = verify_comparison(
        "B) Savannah Reed — Dylan Carter is 40 [chunk:s2:4]; Savannah Reed is 31 [chunk:s2:10], so Savannah.",
        q,
    )
    assert (
        r.changed
        and r.letter == "A"
        and r.text.startswith("A) Dylan Carter — ")
        and "computed" in r.text
    )
    q2 = "Whose birthday comes later in the year, Jasper Harrington or Silas Montgomery?\nA) Silas Montgomery\nB) Jasper Harrington\nC) Both the same\nD) Both incorrect"
    r2 = verify_comparison(
        "A) Silas Montgomery — Jasper Harrington's birthday is December 20th [k], later than Silas Montgomery's April 17th [k2]",
        q2,
    )
    assert r2.changed and r2.letter == "B"
    q3 = "Who is older, Julia Foster or Ethan Parker?\nA) Julia Foster\nB) Ethan Parker\nC) Both the same\nD) Unable to determine"
    r3 = verify_comparison(
        "A) Julia Foster. Both are 28, but her birthday March 12 precedes Ethan's October 14, making her older",
        q3,
    )
    assert r3.changed and r3.letter == "C"
    q4 = "Who has a later birthday, Jaxon Hale or Asher Reid?\nA) Asher Reid\nB) Both the same\nC) Both incorrect\nD) Jaxon Hale"
    r4 = verify_comparison(
        "A) Asher Reid — birthday 2025-01-09 [k], later than Jaxon Hale's 2024-12-09 [k2]", q4
    )
    assert (
        r4.changed and r4.letter == "D"
    )  # month-day within the year: December is later than January
    q5 = "Which event, TasteFest or LaunchPad, has a longer duration?\nA) TasteFest\nB) LaunchPad\nC) Both the same\nD) Both incorrect"
    r5 = verify_comparison(
        "A) TasteFest — LaunchPad lasts nine days vs TasteFest five days [k][k2]", q5
    )
    assert r5.changed and r5.letter == "B"
    # Agreement and missing values leave the answer alone.
    assert (
        verify_comparison("A) Dylan Carter — Dylan Carter is 40; Savannah Reed is 31", q).changed
        is False
    )
    assert verify_comparison("A) Dylan Carter — the memories give no ages", q).changed is False


def test_the_harness_scaffold_does_not_hide_the_comparative() -> None:
    """The MemBench query stamps "Current date: …" above the stem; reading the
    first line as the stem found no comparative and the verifier never ran."""
    from server.memory.domain.ask_compare import comparative_of, verify_comparison

    q = (
        "Current date: 2024-10-05\nWho is older, Dylan Carter or Savannah Reed?\n\n"
        "Choose exactly one option and reply with its letter only.\n"
        "A) Dylan Carter\nB) Savannah Reed\nC) Both the same"
    )
    assert comparative_of(q) == ("older", "number", True)
    r = verify_comparison(
        "B) Savannah Reed — Dylan Carter is 40; Savannah Reed is 31, so Savannah.", q
    )
    assert r.changed and r.letter == "A"


def test_the_other_candidates_clause_is_not_this_candidates_value() -> None:
    """A reader that puts the letter on one line and both heights on the next:
    collapsed to one line, Maya's nearest following value would be Sienna's
    154 and a correct answer would move to "Both the same"."""
    from server.memory.domain.ask_compare import comparative_of, value_near, verify_comparison

    q = (
        "Current date: 2024-10-05\nWho is taller, Sienna Hale or Maya Sinclair?\n\n"
        "Choose exactly one option and reply with its letter only.\n"
        "A) Sienna Hale\nB) Both the same\nC) Maya Sinclair"
    )
    a = "C) Maya Sinclair\nSienna Hale is 154 cm tall, while Maya Sinclair is 175 cm tall, so Maya is taller."
    kind = comparative_of(q)[1]
    assert value_near(a, "Maya Sinclair", kind, ("Sienna Hale",)) == 175
    assert value_near(a, "Sienna Hale", kind, ("Maya Sinclair",)) == 154
    assert verify_comparison(a, q).changed is False
    # and the wrong letter on the same text is still corrected
    r = verify_comparison("A) Sienna Hale\n" + a.split("\n", 1)[1], q)
    assert r.changed and r.letter == "C"
