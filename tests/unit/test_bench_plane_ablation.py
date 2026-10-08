"""bench/plane_ablation.py — a memory-tier ablation run on loom's planes
through the execution_planes mask."""

from __future__ import annotations

import json

from bench.plane_ablation import ArmResult, load_questions, parse_planes, render, score


def test_parse_planes_reads_arms_and_all_means_no_mask() -> None:
    assert parse_planes("fact|fact,chunk| all |") == [["fact"], ["fact", "chunk"], []]
    assert parse_planes("") == parse_planes(
        "fact|fact,chunk|fact,record|fact,lexical|fact,graph|all"
    )


def test_load_questions_accepts_both_dataset_shapes(tmp_path) -> None:
    rr = tmp_path / "rr.json"
    rr.write_text(
        json.dumps(
            {
                "org": "o",
                "namespace": "ns",
                "questions": [{"query": "q1", "gold_keys": ["k1"], "note": "n"}],
            }
        )
    )
    qs, org, ns = load_questions(str(rr))
    assert qs == [{"query": "q1", "gold_keys": ["k1"]}] and (org, ns) == ("o", "ns")

    qa = tmp_path / "qa.json"
    qa.write_text(
        json.dumps(
            [
                {
                    "conversation_id": "c",
                    "conversation": [],
                    "questions": [{"question": "q2", "gold_keys": ["dog"]}],
                }
            ]
        )
    )
    qs, org, ns = load_questions(str(qa))
    assert qs == [{"query": "q2", "gold_keys": ["dog"]}] and (org, ns) == ("", "")


def test_score_accumulates_recall_mrr_abstain_and_latency() -> None:
    arm = ArmResult(planes="fact")
    score(
        {"results": [{"memory_key": "x"}, {"memory_key": "lesson_k1"}]}, ["lesson_k1"], arm, 120.0
    )
    score({"results": [{"memory_key": "x"}], "abstain": True}, ["k9"], arm, 80.0)
    assert (arm.n, arm.hits, arm.abstains) == (2, 1, 1)
    assert arm.recall == 0.5 and arm.mrr == 0.25 and arm.abstain_share == 0.5
    assert arm.p(0.5) in (80.0, 120.0) and arm.p(0.95) == 120.0
    j = arm.to_json()
    assert j["recall"] == 0.5 and j["ch_queries_p50"] is None


def test_render_has_one_row_per_arm() -> None:
    a = ArmResult(planes="fact", n=4, hits=3, rr_sum=2.0)
    b = ArmResult(
        planes="all", n=4, hits=4, rr_sum=3.5, ch_queries_p50=6.0, ch_read_rows_p50=12345.0
    )
    text = render([a, b], 10)
    lines = text.splitlines()
    assert lines[0] == "plane ablation" and "recall@10" in lines[1]
    assert lines[-2].startswith("fact") and lines[-1].startswith("all") and "12,345" in lines[-1]
