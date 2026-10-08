"""bench/membench.py — the pure parts: released-shape normalisation, the
multiple-choice contract, letter parsing, evidence recall, noise interleave."""

from __future__ import annotations

import random

import pytest

from bench.membench import (
    Session,
    build_query,
    evidence_precision,
    evidence_recall,
    interleave_noise,
    parse_iso_date,
    parse_letter,
    render_choice,
    sessions_of,
    stored_chars,
)


def test_dates_and_choices() -> None:
    assert parse_iso_date("'2024-10-01 08:00' Tuesday") == "2024-10-01"
    assert parse_iso_date("2024-10-07 Monday 19:00") == "2024-10-07"
    assert parse_iso_date("") == ""
    assert render_choice(["Alien (1979)", "Naked"]) == "Alien (1979); Naked"
    q = build_query("Who is older?", {"B": "Clara", "A": "Landon"}, "2024-10-06")
    assert q.startswith("Current date: 2024-10-06\nWho is older?")
    assert "A) Landon\nB) Clara" in q and "letter only" in q
    # long options are elided so the query fits memory.ask's 2000-char limit
    long = build_query("Q?", {k: "x" * 900 for k in "ABCD"}, "")
    assert len(long) <= 2000 and long.count("…") == 4


def test_parse_letter_reads_the_leading_letter_before_a_runner_up() -> None:
    # Letter-first answers score the choice, not the
    # runner-up the explanation mentions afterwards.
    assert parse_letter("D) Fear — cue at 07:41; runner-up B fails") == "D"
    assert parse_letter("D) 3 — B and C overstate") == "D"
    # The trap the prompt change removes: prose that names the runner-up first.
    assert parse_letter("Fear — nearest cue 07:41; runner-ups B/C fail; D") == "B"


def test_parse_letter_shapes() -> None:
    assert parse_letter("D") == "D"
    assert parse_letter("D) Clara Jennings") == "D"
    assert parse_letter("(B)") == "B"
    assert parse_letter("The answer is C.") == "C"
    assert parse_letter("A: Maya Carter [m1]") == "A"
    assert parse_letter("I don't have enough information.") == ""
    assert parse_letter("BAD idea") == ""  # a word is not a letter


def test_sessions_of_three_released_shapes() -> None:
    flat = {
        "message_list": [
            {
                "mid": 0,
                "message": "My niece runs TechInnovate.",
                "time": "'2024-10-01 08:00' Tuesday",
            },
            {"mid": 1, "message": "My sister is Amelia.", "time": "'2024-10-02 08:00' Wednesday"},
        ]
    }
    s = sessions_of(flat, "observation")
    assert len(s) == 1 and s[0].date == "2024-10-01" and s[0].step_ids == [(0, 0), (0, 1)]
    assert [m["role"] for m in s[0].messages] == ["user", "user"]

    nested_obs = {
        "message_list": [
            [{"mid": 0, "message": "I love Casablanca.", "time": "'2024-10-01 08:00' Tuesday"}],
            [{"mid": 0, "message": "Titanic again.", "time": "'2024-10-03 08:00' Thursday"}],
        ]
    }
    s = sessions_of(nested_obs, "observation")
    assert [x.sid for x in s] == ["s0", "s1"] and s[1].date == "2024-10-03"
    assert s[1].step_ids == [(1, 0)]

    part = {
        "message_list": [
            [
                {
                    "sid": 117,
                    "user_message": "My sister knits.",
                    "assistant_message": "Lovely!",
                    "time": "'2024-10-01 08:00' Tuesday",
                }
            ],
            [
                {
                    "mid": 3,
                    "user": "Recommend a movie.",
                    "assistant": "Return of the Jedi (1983)!",
                    "time": "'2024-10-02 08:00' Wednesday",
                }
            ],
        ]
    }
    s = sessions_of(part, "participation")
    assert [m["role"] for m in s[0].messages] == ["user", "assistant"]
    assert s[0].step_ids == [(0, 117)] and s[1].step_ids == [(1, 3)]
    assert s[1].messages[1]["content"].startswith("Return of the Jedi")


def test_evidence_recall_counts_sessions_not_keys() -> None:
    step_to_session = {(0, 3): "s0", (1, 9): "s1", (2, 18): "s2"}
    key_to_sessions = {"k1": {"s0"}, "k2": {"s1", "s0"}, "k3": {"s2"}}
    n, got = evidence_recall([(0, 3), (1, 9), (2, 18)], step_to_session, ["k2"], key_to_sessions)
    assert (n, got) == (3, 2)
    n, got = evidence_recall([(0, 3)], step_to_session, [], key_to_sessions)
    assert (n, got) == (1, 0)
    # an evidence id the normaliser never saw is not counted as needed
    assert evidence_recall([(9, 9)], step_to_session, ["k1"], key_to_sessions) == (0, 0)


def test_interleave_noise_keeps_real_order_and_dates_noise() -> None:
    real = [
        Session("s0", "2024-10-01", [{"role": "user", "content": "a"}], step_ids=[(0, 0)]),
        Session("s1", "2024-10-03", [{"role": "user", "content": "b"}], step_ids=[(1, 0)]),
    ]
    noise = [
        Session(f"n{i}", "", [{"role": "user", "content": "news " * 50}], is_noise=True)
        for i in range(10)
    ]
    out = interleave_noise(real, noise, 3, random.Random(1))
    assert len(out) == 5 and sum(s.is_noise for s in out) == 3
    assert [s.sid for s in out if not s.is_noise] == ["s0", "s1"]
    assert all(s.date for s in out if s.is_noise)  # dated by a neighbour
    assert stored_chars(out) > stored_chars(real)
    assert interleave_noise(real, noise, 0, random.Random(1)) == real


def test_item_sampling_is_per_type(tmp_path) -> None:
    """The items drawn for a type must not depend on which other types were
    requested: a --types Aggregative rerun measures the full run's items."""
    import json

    from bench.membench import SCENARIO_DIR, load_items

    base = tmp_path / "Categorical data" / SCENARIO_DIR["observation"]
    base.mkdir(parents=True)
    for qtype in ("Single-hop", "Aggregative"):
        trajs = [
            {
                "tid": i,
                "QA": {
                    "question": f"{qtype} q{i}?",
                    "choices": {"A": "1", "B": "2"},
                    "ground_truth": "A",
                    "target_step_id": [],
                    "time": "2024-01-01",
                },
                "message_list": [{"mid": 0, "role": "user", "content": "hi"}],
            }
            for i in range(30)
        ]
        (base / f"{qtype}.json").write_text(json.dumps({"all": trajs}))
    alone = [it.tid for it in load_items(tmp_path, "observation", ["Aggregative"], 5, 7)]
    with_other = [
        it.tid
        for it in load_items(tmp_path, "observation", ["Single-hop", "Aggregative"], 5, 7)
        if it.qtype == "Aggregative"
    ]
    assert alone == with_other


def test_sessions_carry_the_released_turn_clock() -> None:
    from bench.membench import parse_iso_datetime, sessions_of

    assert parse_iso_datetime("'2024-10-01 08:10' Tuesday") == "2024-10-01T08:10:00"
    assert parse_iso_datetime("") == ""
    traj = {
        "message_list": [
            [
                {
                    "mid": 0,
                    "user": "hi",
                    "assistant": "hello",
                    "time": "'2024-10-01 08:00' Tuesday",
                },
                {
                    "mid": 1,
                    "user": "I felt belittled",
                    "assistant": "sorry",
                    "time": "'2024-10-01 08:01' Tuesday",
                },
            ]
        ]
    }
    s = sessions_of(traj, "participation")[0]
    assert s.date == "2024-10-01"
    assert s.messages[0]["timestamp"] == "2024-10-01T08:00:00"
    assert s.messages[2]["timestamp"] == "2024-10-01T08:01:00"
    assert s.messages[3] == {
        "role": "assistant",
        "content": "sorry",
        "timestamp": "2024-10-01T08:01:00",
    }


def test_item_result_records_abstain_reason_and_degraded() -> None:
    from bench.membench import ItemResult

    r = ItemResult(
        qid="q",
        scenario="observation",
        qtype="Emotion",
        reflective=True,
        letter="",
        ground_truth="D",
        correct=False,
        abstained=True,
        evidence_sessions=1,
        evidence_recalled=0,
        stored_chars=10,
        noise_sessions=0,
        write_seconds=[1.0],
        read_seconds=0.2,
        tokens_in=0,
    )
    assert r.abstain_reason == "" and r.degraded == ""


def test_evidence_precision_counts_only_attributable_keys() -> None:
    step_to_session = {(0, 1): "s0", (0, 9): "s1"}
    key_to_sessions = {"a": {"s0"}, "b": {"s2"}, "c": {"s1"}}
    # a and c come from evidence sessions, b from a noise session, chunk:* is unattributable.
    assert (
        evidence_precision(
            [(0, 1), (0, 9)], step_to_session, ["a", "b", "c", "chunk:s0:1"], key_to_sessions
        )
        == 0.667
    )
    assert evidence_precision([(0, 1)], step_to_session, ["chunk:s0:1"], key_to_sessions) == 0.0
    assert evidence_precision([(0, 1)], step_to_session, ["a"], key_to_sessions) == 1.0


def test_help_renders(capsys: pytest.CaptureFixture[str]) -> None:
    # argparse %-formats every help string; a bare "95% c" raised TypeError.
    from bench import membench

    with pytest.raises(SystemExit) as e:
        membench.main(["--help"])
    assert e.value.code == 0
    assert "95% clustered" in capsys.readouterr().out
