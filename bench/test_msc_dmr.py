"""Pure-function tests for the MSC deep-memory bench — no server, no spend."""

from __future__ import annotations

from datetime import datetime, timedelta

from bench.msc_dmr import (
    ANCHOR,
    JUDGE_PROMPT,
    parse_time_back,
    parse_verdict,
    rouge_l_recall,
    sessions_of,
    summarize,
    turn_to_message,
)


def test_parse_time_back_reads_days_hours_weeks() -> None:
    assert parse_time_back("7 days 8 hours ago") == timedelta(days=7, hours=8)
    assert parse_time_back("1 day ago") == timedelta(days=1)
    assert parse_time_back("2 weeks ago") == timedelta(days=14)
    assert parse_time_back("") == timedelta(0)


def test_sessions_are_dated_from_time_back_and_sorted_oldest_first() -> None:
    row = {
        "previous_dialogs": [
            {"time_back": "1 days ago", "dialog": [{"id": "Speaker 1", "text": "b"}]},
            {"time_back": "7 days 8 hours ago", "dialog": [{"id": "Speaker 2", "text": "a"}]},
        ],
        "dialog": [{"id": "Speaker 1", "text": "c"}],
    }
    sess = sessions_of(row)
    assert [s["turns"][0]["text"] for s in sess] == ["a", "b", "c"]
    assert sess[-1]["date"] == ANCHOR and sess[0]["date"] == ANCHOR - timedelta(days=7, hours=8)


def test_turn_to_message_prefixes_the_speaker_and_stamps_the_minute() -> None:
    m = turn_to_message(
        {"id": "Speaker 2", "text": "I won my fight today."}, datetime(2024, 1, 8, 9, 30)
    )
    assert m == {
        "role": "user",
        "content": "Speaker 2: I won my fight today.",
        "name": "Speaker 2",
        "timestamp": "2024-01-08T09:30:00",
    }


def test_rouge_l_recall_is_lcs_over_gold_length() -> None:
    assert rouge_l_recall("Taylor Swift!", "Taylor Swift") == 1.0
    assert rouge_l_recall("I think it was Taylor, the singer", "Taylor Swift") == 0.5
    assert rouge_l_recall("", "Taylor Swift") == 0.0
    assert rouge_l_recall("anything", "") == 0.0


def test_parse_verdict_follows_the_papers_script() -> None:
    assert parse_verdict("The answer names the necklace. CORRECT") == 1
    assert parse_verdict("It names the mug instead.\nWRONG") == 0
    assert parse_verdict("This is INCORRECT") == 0
    assert parse_verdict("It could be CORRECT or WRONG, I say WRONG") == 0
    assert parse_verdict("no label") is None


def test_judge_prompt_is_the_papers_and_has_the_three_slots() -> None:
    assert JUDGE_PROMPT.count("{question}") == 1
    assert "{gold}" in JUDGE_PROMPT and "{generated}" in JUDGE_PROMPT
    assert JUDGE_PROMPT.startswith(
        "Your task is to label an answer to a question as 'CORRECT' or 'WRONG'."
    )
    assert (
        "Generated answer (WRONG): I'm sorry, I don't remember what you're talking about."
        in JUDGE_PROMPT
    )
    assert JUDGE_PROMPT.rstrip().endswith("or it will break the evaluation script.")


def test_summarize_reports_accuracy_rouge_and_latency_per_arm() -> None:
    rows = [
        {
            "ingest_ms": 100,
            "ask_correct": 1,
            "ask_rouge_l_r": 1.0,
            "ask_ms": 10,
            "digest_correct": 0,
            "digest_rouge_l_r": 0.0,
            "digest_ms": 5,
        },
        {
            "ingest_ms": 300,
            "ask_correct": 0,
            "ask_rouge_l_r": 0.5,
            "ask_ms": 30,
            "digest_correct": 0,
            "digest_rouge_l_r": 0.2,
            "digest_ms": 7,
        },
    ]
    s = summarize(rows, ["ask", "digest", "search"])
    assert s["ask"] == {"accuracy": 0.5, "judged": 2, "rouge_l_r": 0.75, "ms_p50": 30}
    assert s["digest"]["accuracy"] == 0.0 and "search" not in s
    assert s["ingest_ms_p50"] == 300


def test_backoff_matches_the_nested_kv_posture() -> None:
    from bench.msc_dmr import MAX_ATTEMPTS, backoff_seconds

    assert MAX_ATTEMPTS == 8
    assert 150 <= sum(backoff_seconds(a, 503) for a in range(MAX_ATTEMPTS)) <= 240
