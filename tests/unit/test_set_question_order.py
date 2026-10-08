"""An ordering question is a set question: the whole set with its dates."""

from server.memory.domain.synth_policy import is_set_question


def test_order_of_events_questions_are_set_questions() -> None:
    for q in (
        "What is the order of the six museums I visited from earliest to latest?",
        "What is the order of the three sports events I participated in during the past "
        "month, from earliest to latest?",
        "What is the order of the concerts and musical events I attended in the past two "
        "months, starting from the earliest?",
        "In what order did I visit the national parks?",
        "List the trips I took this year in chronological order.",
    ):
        assert is_set_question(q), q


def test_single_fact_questions_stay_single() -> None:
    for q in (
        "When did I order the new couch?",
        "How much did the luxury boots cost?",
        "What did I cook for my friend a couple of days ago?",
    ):
        assert not is_set_question(q), q


def test_full_scan_pool_caps_its_chunk_share_only_when_it_widened() -> None:
    from types import SimpleNamespace

    from server.memory.app.ask.pool import _full_scan_changes

    st = SimpleNamespace(memory_ask_set_full_scan_chunk_share=0.15)
    assert _full_scan_changes(40, 300, st) == {"chunk_cap_share": 0.15}
    assert _full_scan_changes(40, 40, st) == {}
    assert (
        _full_scan_changes(40, 300, SimpleNamespace(memory_ask_set_full_scan_chunk_share=0)) == {}
    )
