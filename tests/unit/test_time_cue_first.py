"""Rows dated inside the question's time cue read first."""

from types import SimpleNamespace

from server.memory.app.ask.retrieval_legs import _time_cue_first


def _m(key, valid_at):
    return SimpleNamespace(memory_key=key, valid_at=valid_at)


def test_rows_inside_a_week_ago_window_move_first_and_nothing_is_dropped() -> None:
    ranked = [
        _m("user_niece_graduation", "2023-06-10"),
        _m("chunk:x:1", "2023-06-15"),
        _m("user_cousin_wedding", "2023-06-15"),
        _m("user_other", None),
    ]
    out = _time_cue_first(
        ranked, 25, "What life event did I participate in a week ago?", "2023-06-22"
    )
    assert [m.memory_key for m in out] == [
        "user_cousin_wedding",
        "user_niece_graduation",
        "chunk:x:1",
        "user_other",
    ]


def test_wide_or_absent_cues_leave_the_order() -> None:
    ranked = [_m("a", "2023-06-10"), _m("b", "2023-06-15")]
    assert _time_cue_first(ranked, 25, "What did I do this year?", "2023-06-22") is ranked
    assert _time_cue_first(ranked, 25, "What is my dog's name?", "2023-06-22") is ranked
    assert _time_cue_first(ranked, 25, "a week ago?", None) is ranked
