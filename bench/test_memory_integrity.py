"""Pure helpers of bench/memory_integrity.py."""

from __future__ import annotations

from bench.memory_integrity import (
    NamespaceIntegrity,
    classify_evidence,
    gold_turns,
    gold_turns_by_question,
    parse_digest_verdict,
    parse_judge_label,
    parse_turn_class,
    session_key_of,
    summarize,
    turn_is_covered,
)

_CONV = {
    "sample_id": "conv-1",
    "conversation": {
        "speaker_a": "Caroline",
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [
            {"speaker": "Caroline", "dia_id": "D1:1", "text": "Hey Mel! Good to see you!"},
            {
                "speaker": "Melanie",
                "dia_id": "D1:2",
                "text": "I went camping with the kids two weekends ago.",
            },
        ],
        "session_2": [
            {
                "speaker": "Caroline",
                "dia_id": "D2:1",
                "text": "I attended my support group yesterday.",
            }
        ],
    },
    "qa": [
        {"question": "q1", "evidence": ["D1:2"]},
        {"question": "q2", "evidence": ["D2:1", "D1:2"]},
        {"question": "adversarial", "evidence": []},
        {"question": "junk", "evidence": ["D9:9"]},
    ],
}


def test_gold_turns_maps_cited_ids_to_turn_text_once() -> None:
    turns = gold_turns(_CONV)
    assert turns == {
        "D1:2": "I went camping with the kids two weekends ago.",
        "D2:1": "I attended my support group yesterday.",
    }


def test_turn_is_covered_matches_the_speaker_prefixed_evidence_row() -> None:
    rows = ["Melanie: I went camping with the kids two weekends ago.", "Caroline: Hey Mel!"]
    assert turn_is_covered("I went camping with the kids two weekends ago.", rows)
    assert not turn_is_covered("I attended my support group yesterday.", rows)
    assert not turn_is_covered("Hey", rows)  # too short to mean anything


def test_classify_evidence_by_scope_set() -> None:
    assert classify_evidence({"items": [{"scope": "cited"}]}) == "cited"
    assert classify_evidence({"items": [{"scope": "cited"}, {"scope": "lexical"}]}) == "lexical"
    assert classify_evidence({"items": [{"scope": "window"}] * 20}) == "window"
    assert classify_evidence({"items": []}) == "none"
    assert classify_evidence({"items": [{"scope": "derived"}, {"scope": "derived"}]}) == "derived"


def test_judge_label_defaults_to_unsupported() -> None:
    assert parse_judge_label("SUPPORTED") == "SUPPORTED"
    assert parse_judge_label(" partial.") == "PARTIAL"
    assert parse_judge_label("no idea") == "UNSUPPORTED"
    assert parse_judge_label("UNSUPPORTED") == "UNSUPPORTED"
    assert parse_judge_label("Unsupported: the memory adds a date.") == "UNSUPPORTED"


def test_summarize_reports_integrity_and_false_memory_rate() -> None:
    a = NamespaceIntegrity(
        sample_id="a",
        namespace="na",
        memories=10,
        by_attribution={"cited": 8, "window": 2},
        gold_turns=20,
        gold_turns_covered=15,
        judged=8,
        supported=6,
        partial=1,
        unsupported=1,
    )
    b = NamespaceIntegrity(
        sample_id="b",
        namespace="nb",
        memories=5,
        by_attribution={"lexical": 5},
        gold_turns=10,
        gold_turns_covered=5,
        judged=5,
        supported=4,
        unsupported=1,
    )
    s = summarize([a, b])
    assert s["memories"] == 15 and s["attribution"] == {"cited": 8, "window": 2, "lexical": 5}
    assert s["attributable_rate"] == round(13 / 15, 4)
    assert s["memory_integrity"] == round(20 / 30, 4)
    assert s["false_memory_rate"] == round(2 / 13, 4) and s["partial_rate"] == round(1 / 13, 4)
    assert a.memory_integrity == 0.75 and b.false_memory_rate == 0.2


def test_gold_turns_by_question_keeps_only_known_ids_in_question_order() -> None:
    assert gold_turns_by_question(_CONV) == [["D1:2"], ["D2:1", "D1:2"]]


def test_turn_class_parses_fact_else_reaction() -> None:
    assert parse_turn_class(" fact.") == "FACT"
    assert parse_turn_class("REACTION") == "REACTION"
    assert parse_turn_class("") == "REACTION"


def test_summarize_reports_question_level_and_fact_bearing_integrity() -> None:
    a = NamespaceIntegrity(
        sample_id="a",
        namespace="na",
        memories=10,
        gold_turns=10,
        gold_turns_covered=6,
        questions=5,
        questions_all_cited=3,
        questions_any_cited=4,
        uncited_fact_bearing=1,
        uncited_reaction=3,
    )
    s = summarize([a])
    assert s["mi_questions_all"] == 0.6 and s["mi_questions_any"] == 0.8
    assert s["uncited_fact_bearing"] == 1 and s["uncited_reaction"] == 3
    # 6 covered of the 7 turns that carried a fact
    assert s["memory_integrity_fact_bearing"] == round(6 / 7, 4)
    assert (
        summarize([NamespaceIntegrity(sample_id="b", namespace="nb")])[
            "memory_integrity_fact_bearing"
        ]
        is None
    )


def test_session_key_and_digest_verdict_parsing() -> None:
    assert session_key_of("locomo-conv-26-abc:session_7") == "session_7"
    assert session_key_of("nope") == ""
    assert parse_digest_verdict("SUPPORTED - all claims present") == ("SUPPORTED", "")
    assert parse_digest_verdict("PARTIAL - adds the specific date 2023-07-06") == (
        "PARTIAL",
        "date",
    )
    assert parse_digest_verdict("Partial: intensifies 'likes' into 'loves'") == ("PARTIAL", "other")
    assert parse_digest_verdict("UNSUPPORTED - claims a trip that never happened") == (
        "UNSUPPORTED",
        "other",
    )
    assert parse_digest_verdict("") == ("UNSUPPORTED", "other")


def test_summarize_reports_digest_fidelity() -> None:
    a = NamespaceIntegrity(
        sample_id="a",
        namespace="na",
        digests=4,
        digest_supported=2,
        digest_partial=2,
        digest_date_precision=2,
    )
    s = summarize([a])
    assert (
        s["digest_fidelity"] == 0.5
        and s["digest_date_precision"] == 2
        and s["digest_unsupported"] == 0
    )
    assert summarize([NamespaceIntegrity(sample_id="b", namespace="nb")])["digest_fidelity"] is None


def test_summarize_reports_derived_row_fidelity() -> None:
    a = NamespaceIntegrity(
        sample_id="a", namespace="na", derived_judged=4, derived_unsupported=1, derived_partial=1
    )
    s = summarize([a])
    assert (
        s["derived_judged"] == 4 and s["derived_fmr"] == 0.25 and s["derived_partial_rate"] == 0.25
    )
    assert summarize([NamespaceIntegrity(sample_id="b", namespace="nb")])["derived_fmr"] is None


def test_check_relative_dates_uses_the_resolver_on_the_session_date() -> None:
    from bench.memory_integrity import check_relative_dates

    # Saturday 2023-07-15: "last Friday" is 07-14; the extractor wrote 07-07.
    entries = [
        {
            "session_id": "ns:session_8",
            "memory_key": "a",
            "value": "Pottery on 2023-07-07 (last Friday).",
        },
        {
            "session_id": "ns:session_8",
            "memory_key": "b",
            "value": "Went hiking yesterday (on 2023-07-14).",
        },
        {
            "session_id": "ns:session_8",
            "memory_key": "c",
            "value": "Started a class last week (around 2023-07-08).",
        },
        {"session_id": "ns:session_8", "memory_key": "d", "value": "Bought a bike two days ago."},
        {
            "session_id": "ns:session_9",
            "memory_key": "e",
            "value": "Ran a race last Saturday (on 2023-07-08).",
        },
    ]
    counts, rows = check_relative_dates(entries, {"session_8": "2023-07-15"})
    # c is a span phrase, d names no ISO day, e has no session date: none count.
    assert counts == {"point": 2, "point_mismatch": 1, "weekday": 1, "weekday_mismatch": 1}
    assert [r["memory_key"] for r in rows] == ["a"]
    assert rows[0]["resolver"] == "2023-07-14" and rows[0]["stored"] == "2023-07-07"


def test_is_derived_entry_by_key_tag_or_trace() -> None:
    from bench.memory_integrity import is_derived_entry

    assert is_derived_entry({"memory_key": "period_digest_session_1_session_5"})
    assert is_derived_entry({"memory_key": "consolidated:the_users_dog"})
    assert is_derived_entry({"memory_key": "x", "tags": ["period_digest", "period:a..b"]})
    assert is_derived_entry({"memory_key": "x", "source_trace": "digest.period|prompt=pd-1"})
    assert not is_derived_entry({"memory_key": "caroline_support_group", "tags": ["speaker:x"]})
    assert not is_derived_entry({"memory_key": "x", "source_trace": "pipeline:set_from_messages"})


def test_period_date_drift_allows_input_dates_and_session_dates_only() -> None:
    from bench.memory_integrity import period_date_drift, source_sessions_of

    digests = {
        "ns:session_1": {
            "summary_text": "On 2023-05-08, Caroline went to a group on 2023-05-07.",
            "valid_at": "2023-05-08T00:00:00",
        },
        "ns:session_2": {"summary_text": "Melanie ran a race.", "valid_at": "2023-05-25T00:00:00"},
    }
    entry = {"tags": ["period_digest", "src:summary:ns:session_1", "src:summary:ns:session_2"]}
    assert source_sessions_of(entry) == ["ns:session_1", "ns:session_2"]
    text = (
        "Between 2023-05-08 and 2023-05-25, Caroline went to a group on 2023-05-07 "
        "and Melanie ran a race on 2023-05-20."
    )
    n, invented = period_date_drift(text, source_sessions_of(entry), digests)
    assert (n, invented) == (4, ["2023-05-20"])
