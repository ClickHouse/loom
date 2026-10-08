"""Time-Dialog through loom — parser, stem, scorers."""

from __future__ import annotations

from datetime import datetime

from bench.time_dialog import (
    final_answer,
    hit_session,
    memory_windows,
    parse_context,
    recall_at,
    retrieval_query,
    score_answer,
)

CTX = (
    "Session 1 happened at 3:18 pm on 8 February, 2020.\n"
    "India Brown: Hi Debra! How's it going?\n"
    "Debra Ryan: Hey India! All's good here.\n"
    "India Brown: I went to a street art fest\nin Brazil.\n"
    "Session 2 happened at 8:35 pm on 21 February, 2020.\n"
    "Debra Ryan: Back again.\n"
)


def test_parse_context_sessions_times_turns():
    ss = parse_context(CTX)
    assert [s["n"] for s in ss] == [1, 2]
    assert ss[0]["time"] == datetime(2020, 2, 8, 15, 18)
    assert ss[0]["turns"][0] == ("India Brown", "Hi Debra! How's it going?")
    # a wrapped line joins the previous turn
    assert ss[0]["turns"][2][1] == "I went to a street art fest in Brazil."
    assert memory_windows(ss[0]["turns"], 2) == [
        "India Brown: Hi Debra! How's it going?\nDebra Ryan: Hey India! All's good here.",
        "India Brown: I went to a street art fest in Brazil.",
    ]


def test_retrieval_query_is_the_stem_except_for_timeline():
    q = "What did India do between April 1 and April 9, 2020?\nA. Fest\nB. Beach\nC. Hike"
    assert retrieval_query(q) == "What did India do between April 1 and April 9, 2020?"
    tl = "Sort these facts.\n(1) A\n(2) B"
    assert retrieval_query(tl, "Timeline") == tl
    assert retrieval_query("When did Elise share a photo?") == "When did Elise share a photo?"


def test_scorers_follow_the_paper():
    assert score_answer("Explicit_Reasoning", "B", "B") == {"f1": 1.0, "em": 1.0}
    assert score_answer("Extract", "B D", "B  C")["f1"] == 0.5
    assert (
        score_answer(
            "Localization", "10:45:00 pm, January 15, 2024", "10:45:41 pm, January 15, 2024"
        )["em"]
        == 1.0
    )
    assert (
        score_answer("Localization", "January 18, 2024", "10:45:41 pm, January 15, 2024")["em"]
        == 0.0
    )
    assert (
        score_answer("Computation", "3 months 9 days", "3 months 8 days 19 hours 11 minutes")["em"]
        == 1.0
    )
    assert score_answer("Computation", "5 months", "3 months 8 days")["em"] == 0.0
    tl = score_answer("Timeline", "(1)(5)(8)(2)(3)(4)(6)(7)", "(1)(5)(8)(2)(3)(4)(7)(6)")
    assert tl["f1"] == 0.75 and tl["em"] == 0.0


def test_final_answer_and_hit_session_and_recall():
    assert final_answer("thinking...\nAnswer: B") == "B"
    assert final_answer("**Answer:** (1)(2)") == "(1)(2)"
    assert final_answer("no marker here") == "no marker here"
    assert hit_session({"session_id": "session_3"}) == "session_3"
    assert hit_session({"memory_key": "s6_c2"}) == "session_6"
    assert hit_session({"memory_key": "summary:x"}) == ""
    assert recall_at(["session_6", "session_1"], {"session_6", "session_9"}, 5) == 0.5
    assert recall_at([], set(), 5) is None
