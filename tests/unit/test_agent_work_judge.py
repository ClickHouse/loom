"""The real-work bench's answer grading: the judge prompt, its verdict, and the checks
that need no judge."""

from __future__ import annotations

from bench.agent_work_judge import answer_score, function_recall, judge_prompt, parse_verdict


def test_the_judge_sees_every_key_point_in_order() -> None:
    ref = {
        "answer": "It happens in X.",
        "key_points": ["X::y reads the mask", "the mask is per part"],
    }
    prompt = judge_prompt("why is the mask ignored?", ref, "Because X::y skips it.")
    assert "1. X::y reads the mask\n2. the mask is per part" in prompt
    assert prompt.rstrip().endswith('"note": "<one sentence>"}')


def test_a_verdict_parses_fenced_or_short_and_a_garbled_one_covers_nothing() -> None:
    v = parse_verdict('```json\n{"covered": [true], "contradicts": false}\n```', 3)
    assert v["covered"] == [True, False, False] and v["contradicts"] is False
    assert parse_verdict("not json", 2)["covered"] == [False, False]


def test_a_contradiction_halves_the_score() -> None:
    assert answer_score({"covered": [True, True, False, False], "contradicts": False}) == 0.5
    assert answer_score({"covered": [True, True, False, False], "contradicts": True}) == 0.25
    assert answer_score({"covered": [], "contradicts": False}) == 0.0


def test_a_function_counts_when_named_with_its_class() -> None:
    funcs = ["DB::MergeTreeData::renameTempPartAndAdd", "DB::Squashing::flush", "executeQuery"]
    answer = "`MergeTreeData::renameTempPartAndAdd` commits it; then `flush` runs; executeQuery starts it."
    # Squashing is never named, so its `flush` does not count
    assert function_recall(answer, funcs) == round(2 / 3, 3)
    assert function_recall(answer, []) is None
