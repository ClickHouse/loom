"""Program-of-thoughts for count questions: the model lists, the server compares."""

from __future__ import annotations

from server.memory.domain import ask_threshold as t

Q2 = (
    "Current date: 2024-10-05\n"
    "How many events last for six weeks or longer?Additionally, How many people do I know who "
    "work in Las Vegas, NV?\n\nChoose exactly one option and reply with its letter only.\n"
    "A) 3 events; 2 people\nB) 5 events; 4 people\nC) 2 events; 3 people\nD) 1 event; 5 people"
)
A2 = """ITEMS (events six weeks or longer):
InnovateEd — six days — fails: too short [summary:s0]
EduStrategic — six weeks — qualifies [summary:s0]
EduInnovate — seven weeks — qualifies
Culinary Fest — six weeks — qualifies
LangFest — a week and a day — fails: too short
ArtVox Fest — nine weeks — qualifies

ITEMS (people working in Las Vegas, NV):
Sofia Whitaker — boss, works in Las Vegas, NV — qualifies
Jasper Caldwell — works in Las Vegas, NV — qualifies
Clara Sinclair — coworker — qualifies
ANSWER: B) 4 events; 3 people — closest; found 4 events and 3 people, no option matches exactly."""


def test_quantities_words_units_articles_and_joins() -> None:
    assert t.parse_quantity("six weeks", temporal=True) == 42
    assert t.parse_quantity("a week and a day", temporal=True) == 8
    assert t.parse_quantity("a whole week", temporal=True) == 7
    assert t.parse_quantity("about four hundred people") == 400
    assert t.parse_quantity("a crowd of about seven hundred people") == 700  # article, not one
    assert t.parse_quantity("600 attendees") == 600
    assert t.parse_quantity("twenty-six") == 26
    assert t.parse_quantity("3,000 people") == 3000
    assert t.parse_quantity("no number here") is None


def test_thresholds_inclusive_exclusive_and_none() -> None:
    assert t.parse_threshold("How many events last for six weeks or longer?") == t.Threshold(
        ">=", 42, True
    )
    assert t.parse_threshold("How many events last for a week or less?") == t.Threshold(
        "<=", 7, True
    )
    assert t.parse_threshold("How many people are aged 26 or below?") == t.Threshold(
        "<=", 26, False
    )
    assert t.parse_threshold("How many events have more than 600 attendees?") == t.Threshold(
        ">", 600, False
    )
    assert t.parse_threshold("How many events last at least eight days?") == t.Threshold(
        ">=", 8, True
    )
    assert t.parse_threshold("How many events are happening in Chicago?") is None
    assert t.Threshold(">=", 42, True).strict() == t.Threshold(">", 42, True)


def test_two_part_answer_is_recomputed_and_the_option_the_strict_count_hits_is_chosen() -> None:
    v = t.verify_counts(A2, Q2)
    assert v.changed and v.inclusive == (4, 3) and v.strict == (2, 3)
    assert v.letter == "C" and v.reading == "strict"
    last = v.text.splitlines()[-1]
    assert last.startswith("ANSWER: C) 2; 3 —") and "strict reading" in last
    # The ITEMS lines are untouched.
    assert "LangFest — a week and a day — fails" in v.text


def test_inclusive_count_wins_when_it_fits_an_option_and_consistent_answers_stay() -> None:
    q = (
        "How many events last for eight days or longer?\n\nChoose one.\n"
        "A) 5 events\nB) 3 events\nC) 2 events\nD) 4 events"
    )
    a = """ITEMS:
Cook & Connect — eight days — qualifies
Poolside Flicks — eight days — qualifies
BookWave — eight days — qualifies
Culinary Lab — seven weeks — qualifies
Culinary Sync — two weeks — qualifies
Cook-Off — one day — fails
ANSWER: A) 5 events — Cook & Connect, Poolside Flicks, BookWave, Culinary Lab, Culinary Sync"""
    v = t.verify_counts(a, q)
    assert v.inclusive == (5,) and v.strict == (2,) and v.changed is False  # A already right
    # Model said D (4): inclusive 5 fits A → corrected to A, inclusive reading.
    v2 = t.verify_counts(a.replace("ANSWER: A) 5 events", "ANSWER: D) 4 events"), q)
    assert v2.changed and v2.letter == "A" and v2.reading == "inclusive"


def test_untouched_without_threshold_options_or_parseable_items() -> None:
    q = "How many events are happening in Chicago?\nA) 9 events\nB) 8 events\nC) 10 events\nD) 7 events"
    a = "ITEMS:\nCraft Fest — Chicago — qualifies\nANSWER: B) 8 events"
    assert t.verify_counts(a, q).changed is False
    q2 = "How many events last for six weeks or longer?"
    assert t.verify_counts("ITEMS:\nX — six weeks — qualifies\nANSWER: 1", q2).changed is False
    q3 = q2 + "\nA) 1 event\nB) 2 events\nC) 3 events"
    assert t.verify_counts("ITEMS:\nX — long — qualifies\nANSWER: A) 1 event", q3).changed is False
    # Parts and ITEMS blocks disagree in number → leave it alone.
    assert t.verify_counts("ITEMS:\nX — six weeks — qualifies\nANSWER: A) 1", Q2).changed is False


def test_literal_count_flips_a_category_exclusion_when_the_value_carries_the_literal() -> None:
    q = "How many events are happening in Boston, MA?\nA) 5 events\nB) 7 events\nC) 6 events\nD) 8 events"
    a = """ITEMS:
Team Connect — Boston, MA (2024-10-02, superseding LA 2024-10-01) — fails: it's a team/crew, not an event
The Policing Forum — Boston, MA — qualifies
SafeComm Launch — Boston, MA — qualifies
SafeNet Forum — Boston, MA — qualifies
ClimbFit23 — Boston, MA — qualifies
Climb & Jam — Boston, MA — qualifies
ClimbFest — Portland, OR (latest, superseding San Francisco) — fails: not in Boston
ANSWER: A) 5 — five Boston events; Team Connect is a crew not an event"""
    v = t.verify_literal_counts(a, q)
    assert v.changed and v.letter == "C" and v.inclusive == (6,)
    assert (
        v.text.splitlines()[-1].startswith("ANSWER: C) 6 —")
        and "category" in v.text.splitlines()[-1]
    )
    # A superseded value never counts, and a non-category failure stays a failure.
    a2 = a.replace(
        "— fails: it's a team/crew, not an event", "— fails: moved to Los Angeles (latest)"
    )
    assert t.verify_literal_counts(a2, q).changed is False
    # Consistent answers and threshold questions are left to verify_counts.
    assert t.verify_literal_counts(a.replace("ANSWER: A) 5", "ANSWER: C) 6"), q).changed is False
    assert (
        t.verify_literal_counts(
            a, "How many events last for a week or less?\nA) 1\nB) 2\nC) 3"
        ).changed
        is False
    )


def test_statement_semantics_rule_is_in_the_count_prompt_and_the_harness_states_it() -> None:
    from bench.membench import STATEMENT_SEMANTICS, build_query
    from server.memory.app.ask.prompts import _COUNT_SYSTEM_RULES

    assert "every statement is ONE item" in _COUNT_SYSTEM_RULES
    q_ent = build_query(
        "How many events are in Chicago?", {"A": "1 events", "B": "2 events"}, "2024-10-05"
    )
    q_stm = build_query(
        "How many events are in Chicago?",
        {"A": "1 events", "B": "2 events"},
        "2024-10-05",
        aggregation="statements",
    )
    assert STATEMENT_SEMANTICS.strip() not in q_ent and STATEMENT_SEMANTICS.strip() in q_stm
    # The stem (first question line) is unchanged so the intent grammar still fires;
    # the convention rides on the contract line.
    assert q_stm.splitlines()[1] == "How many events are in Chicago?"
    assert "letter only. Count every stated instance" in q_stm


def test_statement_count_decides_a_literal_question_when_the_asker_counts_statements() -> None:
    q = (
        "How many events are taking place in Los Angeles, CA?\n\nChoose exactly one option and reply "
        "with its letter only. Count every stated instance separately: repeated or updated statements "
        "about the same item each count once, and names that differ are different items.\n"
        "A) 6 events\nB) 8 events\nC) 7 events\nD) 10 events"
    )
    a = "ITEMS:\nAqua Fest — Los Angeles — qualifies\nANSWER: A) 6 events — Aqua Fest/AquaFest is one entity"
    assert t.wants_statement_count(q)
    v = t.verify_statement_count(a, q, 7)
    assert v.changed and v.letter == "C" and v.reading == "statements"
    assert (
        v.text.splitlines()[-1].startswith("ANSWER: C) 7 —")
        and "7 stated instances name 'Los Angeles'" in v.text
    )
    # Without the asker's convention, or with no counted turns, nothing changes.
    assert (
        t.verify_statement_count(
            a, q.replace(" Count every stated instance separately:", ""), 7
        ).changed
        is False
    )
    assert t.verify_statement_count(a, q, 0).changed is False
    # A threshold question is left to verify_counts even when statements were asked for.
    qt = q.replace(
        "How many events are taking place in Los Angeles, CA?",
        "How many events last a week or less?",
    )
    assert t.verify_statement_count(a, qt, 7).changed is False


def test_capacity_phrasing_is_an_at_least_threshold() -> None:
    assert t.parse_threshold(
        "How many events can fit a crowd of two hundred people?"
    ) == t.Threshold(">=", 200, False)
    assert t.parse_threshold("How many venues have room for 500 people?") == t.Threshold(
        ">=", 500, False
    )
    assert t.parse_threshold("How many events can hold up to 300 people?") == t.Threshold(
        "<=", 300, False
    )


def test_two_part_literal_and_threshold_parts_are_both_computed_with_entity_dedupe() -> None:
    q = (
        "How many people are aged 29 or below?Additionally, How many events are located in Chicago?\n\n"
        "Choose one.\nA) 3 people; 6 events\nB) 2 people; 5 events\nC) 5 people; 7 events\nD) 4 people; 9 events"
    )
    a = """ITEMS (people 29 or below):
Ethan Carter — 29 — qualifies
Savannah Brooks — 28 — qualifies
Logan Pierce — 29 — qualifies
Madison Cole — 28 — qualifies
Chloe Sinclair — 29 — qualifies
Tessa Wynter — 28 — qualifies
Jackson Reed — 40 — fails
ITEMS (events in Chicago):
Cook & Connect — Chicago, IL — qualifies
Cook & Connect — located in Chicago, IL — qualifies
Culinary Sync — Chicago, IL — qualifies
Craft & Cinema — Chicago, IL — qualifies
Craft Fest — Chicago, IL — qualifies
Craft Gala — Chicago, IL — qualifies
Fit Craft Fest — Chicago, IL — qualifies
Team Chicago — based in Chicago, IL — fails: it's a team, not an event
CulinaryX — San Antonio, TX — fails: not Chicago
ANSWER: C) 5 people; 7 events — closest"""
    v = t.verify_counts(a, q)
    # The duplicate Cook & Connect line is one entity; the category-excluded Chicago line counts.
    assert v.inclusive == (6, 7) and v.strict == (3, 7)
    # Neither fits an option here; the answer is left as the model wrote it.
    assert v.changed is False
    q2 = q.replace("A) 3 people; 6 events", "A) 6 people; 7 events")
    v2 = t.verify_counts(a, q2)
    assert v2.changed and v2.letter == "A" and v2.reading == "inclusive"
    assert "computed from the listed items: 6, 7" in v2.text
    # A literal part whose qualifying line does not repeat the literal still counts.
    part = t.ItemsPart(
        lines=[
            ("Clara Sinclair", "coworker", "qualifies"),
            ("Bob", "moved to Denver (latest)", "fails"),
        ]
    )
    assert t._count_literal(part, "Las Vegas") == (1, 2)
    d = t.dedupe_lines(
        t.ItemsPart(
            lines=[
                ("Craft Fest", "x", "qualifies"),
                ("craft-fest", "y", "qualifies"),
                ("Craft Gala", "z", "qualifies"),
            ]
        )
    )
    assert [n for n, _, _ in d.lines] == ["Craft Fest", "Craft Gala"]
