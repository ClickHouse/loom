"""The PerLTQA adapter's pure helpers — reference ids, anchor
MAP, event dates, dialogue messages, the nested QA layout."""

from __future__ import annotations

from bench.perltqa import (
    anchor_map,
    anchor_phrases,
    dialogue_messages,
    ingest_sessions,
    kin_refs,
    parse_event_date,
    qa_records,
    reference_ids,
    refs_of_tags,
    slug,
    summarize,
)


def test_reference_ids_read_both_layouts() -> None:
    rec = {"Reference Memory": "['4_0_0']"}
    assert reference_ids(rec, "events") == ["4_0_0"]
    assert reference_ids({"Reference Memory": ["4_0_0#0", "4_0_1#1"]}, "dialogues") == [
        "4_0_0#0",
        "4_0_1#1",
    ]
    # a profile question names the attribute; the narrative description
    # carries the same fact, so both count as the reference
    assert reference_ids({"Reference Memory": "Gender"}, "profile") == [
        "profile:Gender",
        "profile_description",
    ]
    assert reference_ids({}, "events") == []
    assert reference_ids({"Reference Memory": "1_0"}, "social_relationship") == ["1_0"]


def test_anchor_map_is_the_share_of_anchor_phrases_in_the_answer() -> None:
    rec = {
        "Memory Anchors": [
            {"growing up": [44, 45]},
            {"Marriage and family field": [47, 52]},
            {"": [-1, -1]},
        ]
    }
    anchors = anchor_phrases(rec)
    assert anchors == ["growing up", "Marriage and family field"]
    assert (
        anchor_map("He asked about growing up; the marriage-and-family field came up too.", anchors)
        == 1.0
    )
    assert anchor_map("He asked about growing up.", anchors) == 0.5
    assert anchor_map("", anchors) == 0.0
    assert anchor_map("anything", []) is None
    # a phrase inside another word does not count
    assert anchor_map("regrowing upward", ["growing up"]) == 0.0


def test_event_dates_parse_the_dataset_stamps() -> None:
    assert parse_event_date("May 12, 2022") == "2022-05-12"
    assert parse_event_date("2022-05-12") == "2022-05-12"
    assert parse_event_date("somewhere in 2022-05-12 afternoon") == "2022-05-12"
    assert parse_event_date("") == ""
    assert parse_event_date("last spring") == ""


def test_dialogue_messages_carry_speaker_and_stamp() -> None:
    dl = {
        "events": "1_0_0",
        "contents": {
            "2022-05-12 10:00": [
                "Wang Xiaoming: We're finally here!",
                "Wang Xiaohong: Me too!",
                "",
                "A line with no speaker",
            ]
        },
    }
    msgs = dialogue_messages(dl)
    assert [m["role"] for m in msgs] == ["user"] * 3
    assert msgs[0] == {
        "role": "user",
        "content": "We're finally here!",
        "name": "Wang Xiaoming",
        "timestamp": "2022-05-12T10:00:00",
    }
    assert "name" not in msgs[2] and msgs[2]["content"] == "A line with no speaker"
    assert dialogue_messages({"contents": {}}) == []


def test_qa_records_flatten_the_nested_sections() -> None:
    qa = [
        {
            "Zhang Xiaohong": {
                "profile": [
                    {
                        "Question": "q1",
                        "Answer": "a",
                        "Reference Memory": "Gender",
                        "Memory Anchors": [],
                    }
                ],
                "events": [{"4_0_0": [{"Question": "q2", "Answer": "a", "Memory Anchors": []}]}],
                "dialogues": [
                    {
                        "4_0_0#0": [
                            {"Question": "q3", "Answer": "a", "Reference Memory": "['4_0_0#0']"}
                        ]
                    }
                ],
            }
        }
    ]
    out = qa_records(qa)
    assert list(out) == ["Zhang Xiaohong"]
    assert [r["Question"] for r in out["Zhang Xiaohong"]["events"]] == ["q2"]
    # a nested record without its own reference gets the enclosing id
    assert out["Zhang Xiaohong"]["events"][0]["Reference Memory"] == "['4_0_0']"
    assert reference_ids(out["Zhang Xiaohong"]["dialogues"][0], "dialogues") == ["4_0_0#0"]


def test_sessions_tags_and_summary() -> None:
    mem = {"events": {"4_0_0": {}, "4_0_1": {}}, "dialogues": {"4_0_0#0": {}}}
    assert ingest_sessions(mem) == [
        "-",
        "profile",
        "event-4_0_0",
        "event-4_0_1",
        "dialogue-4_0_0_0",
    ]
    assert refs_of_tags(["ref:4_0_0", "perltqa:event", "ref:profile:Gender"]) == {
        "4_0_0",
        "profile:Gender",
    }
    assert slug("Zhang Xiaohong") == "zhang_xiaohong"
    rows = [
        {
            "section": "events",
            "anchor_map": 1.0,
            "ref_recall": 1.0,
            "cited_ref": 0.0,
            "correct": "yes",
            "abstain": False,
            "ask_ms": 100,
            "search_ms": 10,
        },
        {
            "section": "events",
            "anchor_map": 0.0,
            "ref_recall": 0.0,
            "cited_ref": None,
            "correct": None,
            "abstain": True,
            "ask_ms": 300,
            "search_ms": 30,
        },
        {"section": "profile", "error": "boom"},
    ]
    s = summarize(rows)
    assert s["overall"]["n"] == 3 and s["overall"]["errors"] == 1
    assert s["events"]["anchor_map"] == 0.5 and s["events"]["ref_recall_at_k"] == 0.5
    assert (
        s["events"]["cited_ref"] == 0.0
        and s["events"]["correctness"] == 1.0
        and s["events"]["judged"] == 1
    )
    assert s["events"]["abstain_rate"] == 0.5 and s["events"]["ask_ms_p50"] == 200
    assert s["profile"]["anchor_map"] is None


def test_kin_refs_tie_a_dialogue_to_its_event_both_ways() -> None:
    mem = {"dialogues": {"4_0_0#0": {"events": "4_0_0"}, "4_0_1#1": {"events": "4_0_1"}, "x": {}}}
    kin = kin_refs(mem)
    assert kin["4_0_0#0"] == {"4_0_0"} and kin["4_0_0"] == {"4_0_0#0"}
    assert "x" not in kin
