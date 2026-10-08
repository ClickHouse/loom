"""The answer named an option it called unoffered — set the letter to it."""

from __future__ import annotations

from server.memory.domain.ask_option_guard import parse_options, reconcile_unoffered_claim

Q = (
    "What is the most likely user sentiment around '2024-10-02 13:07' Wednesday\n"
    "A) Disgust\nB) Happiness\nC) Surprise\nD) Fear\n"
    "Reply with the letter of the correct option."
)


def test_parse_options_reads_the_block_and_ignores_prose_letters() -> None:
    assert parse_options(Q) == {"A": "Disgust", "B": "Happiness", "C": "Surprise", "D": "Fear"}
    assert parse_options("I think A is right. B) maybe") == {}


def test_letter_follows_the_named_option_when_the_answer_calls_it_unoffered() -> None:
    a = "D) Surprise — nearest cue is Happiness (not offered), mapping a positive cue [chunk:s0:2]\nITEMS: …"
    r = reconcile_unoffered_claim(a, Q)
    assert r.corrected and r.from_letter == "D" and r.to_letter == "B" and r.named == "Happiness"
    assert r.text.startswith("B) Happiness — nearest cue is Happiness (not offered)")
    assert "letter set to B" in r.text and r.text.endswith("ITEMS: …")

    for shape in (
        "A) Surprise — cue at 14:19 is joy/Happiness (gap 0) which is not offered; positive cue maps to Surprise",
        "D) Sadness — nearest cues are Happiness, but no Happiness option exists so Sadness",
        "C) Surprise — Happiness is not among the options",
    ):
        assert reconcile_unoffered_claim(shape, Q).to_letter == "B", shape


def test_untouched_when_consistent_or_when_the_word_is_truly_unoffered() -> None:
    a = "B) Happiness — cue at 13:07, asked 13:07; runner-up Surprise fails"
    assert reconcile_unoffered_claim(a, Q).corrected is False
    # Sadness really is not an option here: nothing to correct.
    a2 = "A) Disgust — nearest cue is Sadness (not offered); closest negative is Disgust"
    assert reconcile_unoffered_claim(a2, Q).corrected is False
    # No option block in the question → no-op.
    assert reconcile_unoffered_claim(a, "how did I feel at 13:07?").corrected is False
    # No leading letter → no-op.
    assert reconcile_unoffered_claim("Happiness is not offered", Q).corrected is False


def test_missing_letter_is_set_from_a_resolved_datetime_both_halves_or_a_paraphrase() -> None:
    from server.memory.domain.ask_option_guard import (
        pick_option_from_answer,
        reconcile_missing_letter,
    )

    qd = (
        "What is the time for the event located in Boston, MA?\nA) 2024-10-11 Friday 15:00\n"
        "B) 2024-10-12 Friday 14:00\nC) 2024-10-11 Thursday 14:00\nD) 2024-10-11 Friday 14:00"
    )
    a = 'The Boston, MA event is LitEdCon, said "next week on Friday at 2:00 PM." From 2024-10-09, next Friday is 2024-10-11 at 14:00 [chunk:s0:3]'
    r = reconcile_missing_letter(a, qd)
    assert r.corrected and r.to_letter == "D" and r.text.startswith("D) The Boston, MA event")
    # 9:00 AM against 09:00; the anchor date is farther from the time token than the resolved date.
    assert pick_option_from_answer(
        "stated on 2024-10-02 as Wednesday at 9:00 AM, which is 2024-10-16.",
        {
            "A": "2024-10-15 Tuesday 09:00",
            "B": "2024-10-16 Wednesday 10:00",
            "C": "2024-10-16 Wednesday 09:00",
            "D": "2024-10-16 Wednesday 08:00",
        },
    ) == ("C", "datetime")
    assert pick_option_from_answer(
        "BudgetBoost is on 2024-10-16 at 9:00 AM",
        {
            "A": "2024-10-15 Tuesday 09:00",
            "B": "2024-10-16 Wednesday 10:00",
            "C": "2024-10-16 Wednesday 09:00",
            "D": "2024-10-16 Wednesday 08:00",
        },
    ) == ("C", "datetime")
    qc = (
        "Who is older, Owen Brooks or Jasper Hayes?Additionally, Which event has a longer duration?\n"
        "A) Jasper Hayes; TeamSync23\nB) Both the same; Both the same\nC) Owen Brooks; Ocean Bounty🍷\nD) Both incorrect; Both incorrect"
    )
    ac = "Owen Brooks (38) is older than Jasper Hayes (37) [summary:s0]. Ocean Bounty🍷 lasts five days vs TeamSync23's one day."
    assert reconcile_missing_letter(ac, qc).to_letter == "C"
    ql = (
        "What is the most likely description for my sneakers?\n"
        "A) As a Registered Nurse, I'm always on my feet, and the Reebok Classic shoes are a game changer for my long shifts. Their classic design is stylish and comfortable.\n"
        "B) These hiking boots kept my ankles safe on rocky trails; waterproof and rugged for weekend adventures in the mountains.\n"
        "C) The running shoes shaved seconds off my marathon time with responsive cushioning and a breathable mesh upper.\n"
        "D) Elegant leather loafers for the office; polished look, slim profile, and a soft insole for meetings."
    )
    al = "The user's sneakers are the Reebok Classics, described as stylish and comfortable, transforming long shifts for them as a Registered Nurse."
    assert reconcile_missing_letter(al, ql).to_letter == "A"
    # A leading letter is respected; an answer naming nothing stays.
    assert reconcile_missing_letter("D) 2024-10-11 Friday 14:00", qd).corrected is False
    assert reconcile_missing_letter("The memories do not say.", qd).corrected is False


def test_missing_letter_matches_a_month_day_without_the_year() -> None:
    from server.memory.domain.ask_option_guard import pick_option_from_answer

    opts = {
        "A": "2024-10-15 Tuesday 09:00",
        "B": "2024-10-16 Wednesday 10:00",
        "C": "2024-10-15 Tuesday 10:00",
        "D": "2024-10-14 Monday 09:00",
    }
    assert pick_option_from_answer(
        "The Oct 15, 9:00 AM event is Taste Harmony in Portland, OR", opts
    ) == ("A", "datetime")
    assert pick_option_from_answer("The event on 16 October at 10 AM is in Portland", opts) == (
        "B",
        "datetime",
    )
    assert pick_option_from_answer("The event is in Portland", opts) == ("", "")


def test_relative_day_options_resolve_to_dates_from_the_question_date() -> None:
    """The reader states the date
    ("ServiceFest is on 2024-10-13 at 2 PM") while the options say "next week
    Sunday 2:00 PM"; from Current date 2024-10-05, weeks Monday to Sunday, next
    week's Sunday is 2024-10-13 and nextnext week's Wednesday 2024-10-16."""
    from server.memory.domain.ask_option_guard import (
        question_date_of,
        reconcile_missing_letter,
        resolve_relative_day,
    )

    assert resolve_relative_day("next week", "sunday", "2024-10-05") == "2024-10-13"
    assert resolve_relative_day("nextnext week", "wednesday", "2024-10-05") == "2024-10-16"
    assert resolve_relative_day("this week", "sunday", "2024-10-05") == "2024-10-06"
    assert resolve_relative_day("someday", "sunday", "2024-10-05") == ""
    q = (
        "Current date: 2024-10-05\nWhat time is the event happening in Orlando, FL?\n\n"
        "Choose exactly one option and reply with its letter only.\n"
        "A) next week Sunday 2:00 PM\nB) next week Saturday 3:00 PM\nC) this week Sunday 2:00 PM\nD) next week Sunday 1:00 PM"
    )
    assert question_date_of(q) == "2024-10-05"
    a = "ServiceFest in Orlando, FL is on 2024-10-13 at 2 PM [user_servicefest_attendance]; Cycle & Savor is 2024-10-10 at 2:00 PM."
    r = reconcile_missing_letter(a, q)
    assert r.corrected and r.to_letter == "A"
    q2 = (
        "Current date: 2024-10-05\nWhat is the time allocated for an activity that lasts one day?\n\n"
        "Choose exactly one option and reply with its letter only.\n"
        "A) next week Tuesday 2:00 PM\nB) next week Thursday 3:00 PM\nC) nextnext week Wednesday 2:00 PM\nD) nextnext week Wednesday 3:00 PM"
    )
    r2 = reconcile_missing_letter(
        "Sales Synergy is a one-day event on October 16, 2024, at 2:00 PM.", q2
    )
    assert r2.corrected and r2.to_letter == "C"


def test_place_options_are_chosen_by_the_readers_own_places_line() -> None:
    """Participation Post_processing 10 / 472: the reader named the memory's
    city and picked another city's description. Its PLACES line maps each
    option to a city; the program picks the option whose city the memory
    names, and the line leaves the answer."""
    from server.memory.domain.ask_option_guard import reconcile_place_options

    q = (
        "Current date: 2024-10-05\nWhat is the event location description for the activity scheduled on October 12, 2024, at 19:00?\n\n"
        "Choose exactly one option and reply with its letter only.\n"
        "A) Famous for its eco-friendliness and vibrant arts scene.\nB) Known for its theme parks, including Walt Disney World.\n"
        "C) Known for its beautiful beaches and mild climate.\nD) The largest city in the U.S., known for its iconic skyline."
    )
    a = (
        "PLACES: A=Portland; B=Orlando; C=San Diego; D=New York City\n"
        "C) Hike & Climb on 2024-10-12 at 7:00 PM is in Orlando, FL [chunk:s5:0], known for its beaches and mild climate."
    )
    r = reconcile_place_options(a, q)
    assert r.corrected and r.from_letter == "C" and r.to_letter == "B" and r.named == "orlando"
    assert r.text.startswith("B) Known for its theme parks") and "PLACES" not in r.text
    # agrees → the line is still stripped, nothing else changes
    r2 = reconcile_place_options(
        "PLACES: A=Portland; B=Orlando\nB) Hike & Climb is in Orlando, FL.", q
    )
    assert not r2.corrected and r2.text.startswith("B) Hike & Climb") and "PLACES" not in r2.text
    # no PLACES line → untouched
    assert reconcile_place_options("C) something in Orlando", q).text == "C) something in Orlando"


def test_a_stated_date_overrides_the_readers_relative_phrase() -> None:
    from server.memory.domain.ask_option_guard import verify_relative_date_option

    q = (
        "Current date: 2024-10-05\nWhat time is the event happening in Orlando, FL?\n\n"
        "Choose exactly one option and reply with its letter only.\n"
        "A) next week Sunday 2:00 PM\nB) next week Saturday 3:00 PM\nC) this week Sunday 2:00 PM\nD) next week Sunday 1:00 PM"
    )
    a = "C) this week Sunday 2:00 PM — ServiceFest in Orlando, FL is on October 13, 2024 at 2 PM [summary:s5]."
    r = verify_relative_date_option(a, q)
    assert (
        r.corrected
        and r.from_letter == "C"
        and r.to_letter == "A"
        and r.text.startswith("A) next week Sunday 2:00 PM")
    )
    # agrees, or no date stated → untouched
    assert (
        verify_relative_date_option(
            "A) next week Sunday 2:00 PM — ServiceFest is on 2024-10-13 at 2 PM.", q
        ).corrected
        is False
    )
    assert (
        verify_relative_date_option(
            "C) this week Sunday 2:00 PM — ServiceFest is on Sunday at 2 PM.", q
        ).corrected
        is False
    )


def test_an_entitys_city_comes_from_the_pool_not_a_neighbour() -> None:
    """The reader puts the Success Gala in Denver while the pool holds
    'Success Gala is happening in San Diego, CA'; the answer's city is replaced
    by the pool's, and the PLACES map then picks the option for San Diego."""
    from types import SimpleNamespace as H

    from server.memory.domain.ask_option_guard import (
        reconcile_place_options,
        verify_entity_location,
    )

    pool = [
        H(
            memory_key="user_success_gala_duration",
            content_excerpt="User found out that the Success Gala lasts only one day.",
        ),
        H(
            memory_key="success_gala_locat_in_location",
            content_excerpt="User found out that the Success Gala is happening in San Diego, CA.",
        ),
        H(
            memory_key="user_eco_beach_bash",
            content_excerpt="User is going to attend Eco Beach Bash in Denver, CO.",
        ),
    ]
    q = (
        "Current date: 2024-10-05\nIs there an activity that lasts one day and matches the description of its location?\n\n"
        "Choose exactly one option and reply with its letter only.\n"
        "A) Known for its beautiful beaches and mild climate.\nB) Famous for its eco-friendliness and vibrant arts scene.\n"
        "C) Known for its beaches, nightlife, and multicultural atmosphere.\nD) The largest city in the U.S., known for its iconic skyline."
    )
    a = "B) The one-day event is Success Gala, which is in Denver, CO, known for eco-friendliness [user_success_gala_duration]\nPLACES: A=San Diego; B=Denver; C=Miami; D=New York City"
    r = verify_entity_location(a, q, pool)
    assert (
        r.corrected
        and "San Diego" in r.text.split("\n")[0]
        and "Denver, CO" not in r.text.split("\n")[0]
    )
    r2 = reconcile_place_options(r.text, q)
    assert r2.corrected and r2.to_letter == "A"
    # the pool agrees with the answer → untouched
    assert (
        verify_entity_location("A) Success Gala is in San Diego, CA.", q, pool).corrected is False
    )


def test_a_letterless_answer_that_states_a_date_picks_the_relative_option() -> None:
    from server.memory.domain.ask_option_guard import reconcile_missing_letter

    q = (
        "Current date: 2024-10-04\nHow long is the activity that lasts for four weeks?\n\n"
        "Choose exactly one option and reply with its letter only.\n"
        "A) next week Friday 6:00 PM\nB) next week Thursday 7:00 PM\nC) two weeks from now Friday 7:00 PM\nD) next week Friday 7:00 PM"
    )
    a = "The four-week activities are Fit Travel Fest and FitFest2024; FitFest2024 is happening on October 11th, 2024, at 7 PM [user_fitfest2024_attendance]. None of the options seem to match."
    r = reconcile_missing_letter(a, q)
    assert r.corrected and r.to_letter == "D" and r.text.startswith("D) ")


def test_need_more_answers_lead_with_the_derived_figure() -> None:
    from server.memory.domain.ask_option_guard import lead_with_needed_figure

    q = "How many points do I need to earn to redeem a free skincare product at Sephora?"
    a = "You need a total of 300 points [k1]. As of 2023-05-21 you had 200 points, meaning you need 100 more."
    r = lead_with_needed_figure(a, q)
    assert r.corrected and r.text.startswith("100 more — You need a total of 300")
    # already leads with a figure, or no derived figure, or not a need-more question: unchanged
    assert not lead_with_needed_figure("100 more points [k1].", q).corrected
    assert not lead_with_needed_figure("You need a total of 300 points.", q).corrected
    assert not lead_with_needed_figure(a, "What is the capital of Italy?").corrected
