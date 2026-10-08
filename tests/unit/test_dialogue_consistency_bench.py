"""bench/dialogue_consistency.py — consistency is scored by turn, not by recall
. Pure plumbing: no network, no LLM."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from bench.dialogue_consistency import (
    FIXTURE,
    Dialogue,
    FakeLoom,
    TurnRow,
    baseline_quality,
    compare_runs,
    complexity,
    consistency_score,
    entailment_rate,
    hard_subset,
    heuristic_verdict,
    load_fixture,
    longmemeval_to_dialogues,
    main,
    parse_verdict,
    read_rows,
    run_dialogue,
    summarize,
    turn_curve,
    write_rows,
)


def test_consistency_score_is_the_paper_formula_clamped() -> None:
    assert consistency_score(1.0, 0.0) == 1.0
    assert consistency_score(0.0, 1.0) == 0.0
    assert consistency_score(0.0, 0.0) == 0.5
    assert consistency_score(0.9, 0.1) == pytest.approx(0.9)
    assert consistency_score(2.0, -1.0) == 1.0  # clamped, never > 1


def test_entailment_rate_counts_only_entailed() -> None:
    assert entailment_rate(["entailed", "neutral", "contradicted", "entailed"]) == 0.5
    assert entailment_rate([]) == 0.0


def test_parse_verdict_tolerates_prose_and_fences_and_missing_label() -> None:
    v = parse_verdict(
        'Sure! Here it is:\n```json\n{"label": "Contradicted", "p_entail": 0.1, '
        '"p_contradict": 0.9, "quality": 0.4, "contradicted_span": "60%", "rationale": "8 not 6"}\n```'
    )
    assert v.label == "contradicted" and v.p_contradict == 0.9 and v.contradicted_span == "60%"
    # Label missing → derived from the probabilities.
    assert parse_verdict('{"p_entail": 0.92, "p_contradict": 0.02}').label == "entailed"
    assert parse_verdict('{"p_entail": 0.3, "p_contradict": 0.2}').label == "neutral"
    # Garbage → neutral, never an exception, never a contradiction.
    g = parse_verdict("I cannot judge this.")
    assert g.label == "neutral" and g.p_contradict == 0.0
    assert parse_verdict('{"p_entail": "many"}').p_entail == 0.0


def _row(d: str, t: int, cs: float, label: str, q: float) -> TurnRow:
    return TurnRow("r", d, "x", t, 3, "q", "a", "", 10, "synth", "cheap", False, label, 0, 0, cs, q)


def test_turn_curve_buckets_by_turn_index() -> None:
    rows = [
        _row("d1", 0, 1.0, "entailed", 1.0),
        _row("d2", 0, 0.0, "contradicted", 0.5),
        _row("d1", 1, 0.5, "neutral", 0.5),
    ]
    curve = turn_curve(rows)
    assert [c["turn_index"] for c in curve] == [0, 1]
    assert curve[0]["n"] == 2 and curve[0]["mean_cs"] == 0.5 and curve[0]["der"] == 0.5
    assert curve[0]["contradicted"] == 1 and curve[1]["n"] == 1
    s = summarize(rows)
    assert s["n_dialogues"] == 2 and s["der"] == pytest.approx(1 / 3, abs=1e-3)


def test_fixture_loads_and_covers_the_paper_task_types() -> None:
    ds = load_fixture()
    assert 5 <= len(ds) <= 8
    assert all(3 <= len(d.turns) <= 6 for d in ds)
    kinds = {d.task_type for d in ds}
    assert {"self_correction", "self_affirmation", "topic_shift"} <= kinds
    assert any("math" in k or "reasoning" in k for k in kinds)


def test_hard_subset_is_deterministic_and_prints_auditable_stats() -> None:
    ds = load_fixture()
    refined, stats = hard_subset(ds, baseline=None, fraction=0.5)
    again, _ = hard_subset(ds, baseline=None, fraction=0.5)
    assert [d.dialogue_id for d in refined] == [d.dialogue_id for d in again]
    assert stats["n_refined"] == round(len(ds) * 0.5)
    assert set(stats["original"]) == set(stats["refined"])
    # The refined set is the top of the ranking, and the ranking is total.
    ranked = [r["dialogue_id"] for r in stats["ranking"]]
    assert ranked[: len(refined)] == [d.dialogue_id for d in refined]
    assert len(ranked) == len(ds)
    # Quality band drops ceiling/floor dialogues before ranking.
    base = {d.dialogue_id: (0.95 if i % 2 else 0.6) for i, d in enumerate(ds)}
    banded, st2 = hard_subset(ds, baseline=base, band=(0.4, 0.8), fraction=1.0)
    assert st2["n_after_band"] == sum(1 for v in base.values() if v <= 0.8)
    assert all(base[d.dialogue_id] <= 0.8 for d in banded)


def test_complexity_features_move_in_the_right_direction() -> None:
    plain = Dialogue("p", "x", ["hello there", "how are you"])
    dense = Dialogue(
        "d",
        "x",
        [
            "If Room 1 is occupied then Room 2 is not. Priya owns Nova, due 15 November.",
            "Unless Acme confirms, the Globex deadline cannot move to 3 March.",
        ],
    )
    cp, cd = complexity(plain), complexity(dense)
    assert cd.unique_entities > cp.unique_entities
    assert cd.conditional_freq > cp.conditional_freq
    assert cd.negative_constraint_freq > cp.negative_constraint_freq
    assert cd.avg_words_per_turn > cp.avg_words_per_turn


def test_longmemeval_reshape_uses_evidence_sessions_and_ends_with_the_question() -> None:
    item = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What degree did I get?",
        "answer_session_ids": ["s2"],
        "haystack_session_ids": ["s1", "s2"],
        "haystack_sessions": [
            [{"role": "user", "content": "noise"}, {"role": "assistant", "content": "ok"}],
            [
                {"role": "user", "content": "I graduated in Business Administration."},
                {"role": "assistant", "content": "Congrats"},
                {"role": "user", "content": "In 2019."},
            ],
        ],
    }
    ds = longmemeval_to_dialogues(
        [
            item,
            {
                "question_id": "q2",
                "question": "x",
                "answer_session_ids": [],
                "haystack_session_ids": [],
                "haystack_sessions": [],
            },
        ],
        max_turns=6,
    )
    assert len(ds) == 1
    assert ds[0].turns == [
        "I graduated in Business Administration.",
        "In 2019.",
        "What degree did I get?",
    ]
    assert ds[0].task_type == "single-session-user"


def test_heuristic_judge_is_a_plumbing_stub_with_the_three_labels() -> None:
    assert (
        heuristic_verdict("The deadline is 15 November.", "The deadline is 15 November.").label
        == "entailed"
    )
    assert (
        heuristic_verdict("Room 2 is occupied.", "Room 2 is not occupied.").label == "contradicted"
    )
    assert heuristic_verdict("hello", "quantum chromodynamics lattice").label == "neutral"


def test_run_dialogue_is_autoregressive_and_rows_round_trip_csv(tmp_path: Path) -> None:
    d = Dialogue("d1", "t", ["Alpha is 3.", "Beta is 4.", "What are Alpha and Beta?"])
    loom = FakeLoom()
    seen: list[str] = []

    def judge(premise: str, hypothesis: str):
        seen.append(premise)
        return heuristic_verdict(premise, hypothesis)

    rows = run_dialogue(loom, d, run_id="abcdef123456", namespace_prefix="dc", judge=judge)
    assert [r.turn_index for r in rows] == [0, 1, 2]
    # Turn 2's premise contains loom's OWN earlier answers (the assistant turns).
    assert "[assistant] Based on what you said" in seen[2]
    # The fake wrote the assistant turn back before the next user turn.
    assert len(loom.turns) == 1
    out = tmp_path / "rows.csv"
    write_rows(out, rows)
    back = read_rows(out)
    assert [(r.dialogue_id, r.turn_index, r.cs) for r in back] == [
        (r.dialogue_id, r.turn_index, r.cs) for r in rows
    ]
    assert baseline_quality(out)["d1"] == pytest.approx(sum(r.quality for r in rows) / 3)


def test_compare_pairs_by_dialogue_and_turn_and_clusters_by_dialogue() -> None:
    a = [
        _row("d1", 0, 0.5, "neutral", 0.5),
        _row("d1", 1, 0.5, "neutral", 0.5),
        _row("d2", 0, 0.5, "neutral", 0.5),
    ]
    b = [
        _row("d1", 0, 1.0, "entailed", 1.0),
        _row("d1", 1, 1.0, "entailed", 1.0),
        _row("d2", 0, 1.0, "entailed", 1.0),
        _row("d3", 0, 0.0, "contradicted", 0.0),
    ]  # unpaired: ignored
    res = compare_runs(a, b)
    assert res["cs"].n == 3 and res["cs"].clusters == 2
    assert res["cs"].mean == pytest.approx(0.5) and res["der"].mean == pytest.approx(1.0)
    assert res["quality"].wins == 3 and res["quality"].losses == 0


def test_cli_dry_run_writes_rows_and_curve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "dc.csv"
    curve = tmp_path / "curve.json"
    assert main(["--dry-run", "--out", str(out), "--curve-out", str(curve), "--limit", "3"]) == 0
    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len({r["dialogue_id"] for r in rows}) == 3
    payload = json.loads(curve.read_text(encoding="utf-8"))
    assert payload["n_dialogues"] == 3 and payload["curve"][0]["turn_index"] == 0
    assert FIXTURE.exists()
    # compare mode against itself: every delta is a tie.
    assert main(["--compare", str(out), str(out)]) == 0
    assert "ties" in capsys.readouterr().out
