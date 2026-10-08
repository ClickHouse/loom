"""The latest statement wins, decided from the evidence clocks."""

from __future__ import annotations

from types import SimpleNamespace as H

from server.memory.domain.ask_latest_value import latest_clocks, prefer_latest_statement
from server.memory.domain.ask_option_guard import parse_options

Q = "What is the position of my father?\nA) Corporate Accountant\nB) Corporate Attorney\nC) Corporate Consultant\nD) Corporate Paralegal"
HITS = [
    H(
        memory_key="chunk:s7:2",
        content_excerpt="[assistant @ 2024-10-05 07:55] Great to hear about your dad's work! [user @ 2024-10-05 07:56] My dad works as a corporate paralegal.",
        valid_at="2024-10-05T07:51:00",
    ),
    H(
        memory_key="nolan_work",
        content_excerpt='"Nolan works as a corporate attorney at Pioneer Legal Group LLC." (#nolan_work)',
        valid_at="2024-10-05T08:00:00",
    ),
    H(
        memory_key="summary:s7",
        content_excerpt="On 2024-10-05 the user said their father is a corporate attorney.",
        valid_at="2024-10-05T00:00:00",
    ),
]


def test_clocks_read_user_turns_and_fact_dates_not_assistant_or_summaries() -> None:
    clocks = latest_clocks(
        {"B": "Corporate Attorney", "D": "Corporate Paralegal", "A": "Corporate Accountant"}, HITS
    )
    assert clocks == {"B": "2024-10-05 08:00", "D": "2024-10-05 07:56"}
    # An assistant echo is not a statement.
    echo = [
        H(
            memory_key="chunk:s0:0",
            content_excerpt="[assistant @ 2024-10-05 09:00] a corporate paralegal indeed",
            valid_at="",
        )
    ]
    assert latest_clocks({"D": "Corporate Paralegal"}, echo) == {}


def test_reader_choice_stated_earlier_yields_to_the_later_statement() -> None:
    a = "D) Corporate Paralegal — the user directly stated it [chunk:s7:2]; the attorney memories conflict."
    r = prefer_latest_statement(a, Q, HITS)
    assert r.changed and r.from_letter == "D" and r.to_letter == "B"
    assert r.text.startswith("B) Corporate Attorney — the latest statement (2024-10-05 08:00)")
    assert "supersedes 'Corporate Paralegal' (2024-10-05 07:56)" in r.text and "Reader:" in r.text
    # The latest option chosen → untouched; no leading letter → untouched; a tie → untouched.
    assert prefer_latest_statement("B) Corporate Attorney [nolan_work]", Q, HITS).changed is False
    assert prefer_latest_statement("Corporate Paralegal, per the chunk", Q, HITS).changed is False
    tie = HITS[:1] + [
        H(
            memory_key="nolan_work",
            content_excerpt="corporate attorney",
            valid_at="2024-10-05T07:56:00",
        )
    ]
    assert prefer_latest_statement(a, Q, tie).changed is False


def test_comparisons_are_not_value_updates() -> None:
    from server.memory.domain.ask_latest_value import is_value_update_question
    from server.memory.domain.ask_option_guard import parse_options

    qc = "Who is older, Dylan Carter or Savannah Reed?\nA) Dylan Carter\nB) Savannah Reed\nC) Both the same\nD) Both incorrect"
    assert is_value_update_question(qc, parse_options(qc)) is False
    qe = "Which event, TasteFest or LaunchPad, lasts longer?\nA) TasteFest\nB) LaunchPad\nC) Unable to determine\nD) Both the same"
    assert is_value_update_question(qe, parse_options(qe)) is False
    assert is_value_update_question(Q, parse_options(Q)) is True
    # A comparison never moves, even when one candidate's name was stated later.
    hits = [
        H(
            memory_key="chunk:s2:4",
            content_excerpt="[user @ 2024-10-01 09:50] Dylan Carter is 40.",
            valid_at="",
        ),
        H(
            memory_key="chunk:s2:10",
            content_excerpt="[user @ 2024-10-01 09:56] Savannah Reed is 31.",
            valid_at="",
        ),
    ]
    assert prefer_latest_statement("A) Dylan Carter — 40 vs 31", qc, hits).changed is False


def test_a_named_subject_gates_the_mentions_that_count() -> None:
    """WanderLit's scale is corrected 250 → 200 on 10-01; a fact about Budget
    Boost ("three hundred people", 10-06) joins the pool through option
    completion and must not move the answer. Only statements naming
    WanderLit count."""
    from server.memory.domain.ask_latest_value import subject_terms

    q = (
        "What exactly is WanderLit's scale?\nA) one hundred fifty people"
        "\nB) two hundred fifty people\nC) two hundred people\nD) three hundred people"
    )
    assert subject_terms(q) == ["wanderlit"]
    hits = [
        H(
            memory_key="chunk:s0:0",
            content_excerpt="[user @ 2024-10-01 08:02] I found out that WanderLit has a scale of two hundred and fifty people. [user @ 2024-10-01 08:07] I just wanted to clarify that WanderLit has a scale of two hundred people.",
            valid_at="",
        ),
        H(
            memory_key="user_wanderlit_attendance_2",
            content_excerpt='"WanderLit has a scale of two hundred people."',
            valid_at="2024-10-01T08:07:00",
        ),
        H(
            memory_key="user_budget_boost_attendance_count",
            content_excerpt='"Around three hundred people are expected to attend Budget Boost."',
            valid_at="2024-10-06T10:26:00",
        ),
    ]
    assert latest_clocks(parse_options(q), hits, q) == {"C": "2024-10-01 08:07"}
    assert (
        prefer_latest_statement(
            "C) two hundred people [user_wanderlit_attendance_2]", q, hits
        ).changed
        is False
    )
    # No proper noun in the stem: nothing is gated (378 still flips).
    assert subject_terms(Q) == []
    assert prefer_latest_statement("D) Corporate Paralegal", Q, HITS).to_letter == "B"


def test_the_harness_scaffold_is_not_a_subject() -> None:
    from server.memory.domain.ask_latest_value import is_value_update_question, subject_terms

    q = (
        "Current date: 2024-10-05\n"
        + Q.split("\n")[0]
        + "\n\nChoose exactly one option and reply with its letter only.\n"
        + "\n".join(Q.split("\n")[1:])
    )
    assert subject_terms(q) == []
    assert is_value_update_question(q, parse_options(q)) is True
    assert prefer_latest_statement("D) Corporate Paralegal", q, HITS).to_letter == "B"
    q40 = "Current date: 2024-10-05\nWhat exactly is WanderLit's scale?\n\nChoose exactly one option and reply with its letter only.\nA) one hundred fifty people\nB) two hundred people"
    assert subject_terms(q40) == ["wanderlit"]
