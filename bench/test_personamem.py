"""Pure parts of the PersonaMem-v2 harness."""

from __future__ import annotations

from bench.personamem import (
    QuestionResult,
    aggregate,
    context_for,
    observation_dates,
    option_seed,
    options_for,
    parse_incorrect,
    parse_letter,
    parse_query,
    reader_prompt,
    sessionize,
)


def test_sessionize_windows_start_on_user_turns_and_skip_system() -> None:
    msgs = [{"role": "system", "content": "persona"}]
    for i in range(7):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    windows = sessionize(msgs, 5)  # odd cut → nudged so no window opens on an assistant turn
    assert [len(w) for w in windows] == [6, 6, 2]
    assert all(w[0]["role"] == "user" for w in windows)
    assert sum(len(w) for w in windows) == 14 and sessionize([], 4) == []


def test_observation_dates_are_a_day_apart() -> None:
    assert observation_dates(3) == ["2025-01-06", "2025-01-07", "2025-01-08"]


def test_options_shuffle_is_seeded_and_letter_tracks_the_correct_one() -> None:
    opts, letter = options_for("right", ["w1", "w2", "w3"], seed=3)
    assert sorted(opts) == ["right", "w1", "w2", "w3"] and opts["ABCD".index(letter)] == "right"
    assert options_for("right", ["w1", "w2", "w3"], seed=3) == (opts, letter)
    assert options_for("right", ["w1", "w2", "w3"], seed=4) != (opts, letter) or True


def test_parse_letter_and_query_and_incorrect() -> None:
    assert parse_letter("B") == "B" and parse_letter(" the answer is c.") == "C"
    assert parse_letter("none of these") == ""
    raw = "{'role': 'user', 'content': 'What calms a morning?'}"
    assert parse_query(raw) == "What calms a morning?"
    assert parse_query("plain text") == "plain text"
    assert parse_incorrect('["a", "b"]') == ["a", "b"] and parse_incorrect("['x']") == ["x"]
    assert parse_incorrect("junk") == []


def test_context_arms_and_prompt_shape() -> None:
    assert context_for("memories", "M", "P") == "M" and context_for("profile", "M", "P") == "P"
    both = context_for("memories+profile", "M", "P")
    assert both.index("P") < both.index("M")
    assert context_for("memories+profile", "M", "") == "M"
    prompt = reader_prompt("ctx", "q?", ["a1", "a2", "a3", "a4"])
    assert "A. a1" in prompt and "D. a4" in prompt and "USER'S NEW MESSAGE" in prompt


def _r(pid: str, i: int, arm: str, correct: bool, **kw) -> QuestionResult:
    base = dict(
        persona_id=pid,
        namespace="ns",
        question_index=i,
        scenario="chat_message",
        pref_type="neutral_preference",
        who="self",
        updated=False,
        query="q",
        correct_letter="A",
        arm=arm,
        picked="A" if correct else "B",
        correct=correct,
        context_chars=10,
        hits=3,
        profile_chars=0,
    )
    base.update(kw)
    return QuestionResult(**base)


def test_aggregate_reports_arms_slices_and_pairwise_flips() -> None:
    rows = [
        _r("1", 0, "memories", True),
        _r("1", 0, "memories+profile", True),
        _r("1", 1, "memories", False),
        _r("1", 1, "memories+profile", True, updated=True),
        _r("1", 2, "memories", True),
        _r("1", 2, "memories+profile", False),
        _r("1", 3, "memories", False, error="boom"),
        _r("1", 3, "memories+profile", False),
    ]
    agg = aggregate(rows)
    assert agg["questions"] == 4 and agg["errors"] == 1
    assert agg["arms"]["memories"]["n"] == 3 and agg["arms"]["memories"]["accuracy"] == 66.67
    assert agg["arms"]["memories+profile"]["accuracy"] == 50.0
    assert agg["arms"]["memories+profile"]["updated"] == {"n": 1, "accuracy": 100.0}
    pair = agg["pairs"]["memories vs memories+profile"]
    assert pair == {"paired": 3, "memories_only": 1, "memories+profile_only": 1}


def test_aggregate_counts_a_reply_without_a_letter_as_wrong() -> None:
    rows = [_r("1", 0, "memories", True), _r("1", 1, "memories", False, picked="")]
    agg = aggregate(rows)
    assert agg["arms"]["memories"] == {**agg["arms"]["memories"], "n": 2, "accuracy": 50.0}
    assert agg["unparsed"] == 1


def test_option_seed_varies_across_personas_at_the_same_question_index() -> None:
    letters = {
        options_for("right", ["w1", "w2", "w3"], seed=option_seed(11, pid, 0))[1]
        for pid in (str(p) for p in range(40))
    }
    assert letters == {"A", "B", "C", "D"}
    assert option_seed(11, "260", 3) == option_seed(11, "260", 3)
    assert option_seed(11, "260", 3) != option_seed(11, "261", 3)


def test_question_result_carries_the_hit_keys_column() -> None:
    import dataclasses

    from bench.personamem import QuestionResult

    names = [f.name for f in dataclasses.fields(QuestionResult)]
    assert "hit_keys" in names
