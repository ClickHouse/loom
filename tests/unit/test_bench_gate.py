"""bench/_gate.py — the accuracy gate every judged bench reads: majority
labels, per-item flip rate, the paired clustered bootstrap
over majority labels, the pass|fail|unresolved verdict, the judge TPR/FPR
correction, and the evaluation-read defaults."""

from __future__ import annotations

import argparse
import random

import pytest

from bench._gate import (
    ASK_MEASUREMENT_FIELDS,
    DEFAULT_BAND_PP,
    SEARCH_MEASUREMENT_FIELDS,
    JudgeCalibration,
    add_gate_arguments,
    agreement,
    band_from_null,
    flip_rate,
    gate_from_args,
    identical_share,
    is_unstable,
    labels_by_key,
    majority_label,
    measurement_fields,
    normalize_label,
    paired_gate,
    parse_calibration,
    verdict_for,
)

# ------------------------------------------------------------------ labels


def test_majority_is_conservative_on_ties_and_outvotes_errors() -> None:
    assert majority_label(["CORRECT", "CORRECT", "WRONG"]) == "CORRECT"
    assert majority_label(["CORRECT", "WRONG"]) == "WRONG"  # tie → WRONG
    assert majority_label(["CORRECT", "ERROR", "ERROR"]) == "CORRECT"
    assert majority_label(["ERROR", "ERROR"]) == "ERROR"
    assert majority_label([]) == "WRONG"
    # other benches' vocabularies fold into the same vote
    assert majority_label(["correct", "wrong", "correct"]) == "CORRECT"
    assert majority_label([True, False, False]) == "WRONG"  # type: ignore[list-item]
    assert majority_label(["abstained", "correct"]) == "WRONG"


def test_normalize_label_covers_bench_vocabularies() -> None:
    assert normalize_label("correct") == "CORRECT"
    assert normalize_label("abstained") == "WRONG"
    assert normalize_label(True) == "CORRECT"
    assert normalize_label("") == "ERROR"
    assert normalize_label("banana") == "ERROR"


def test_flip_rate_counts_only_repeated_items() -> None:
    assert flip_rate([["CORRECT"], ["WRONG"]]) is None  # single trials: nothing to flip
    lists = [
        ["CORRECT", "CORRECT", "CORRECT"],
        ["CORRECT", "WRONG", "CORRECT"],
        ["WRONG", "WRONG", "ERROR"],  # judged runs agree; the error is not a flip
        ["CORRECT", "WRONG"],
    ]
    assert flip_rate(lists) == 0.5
    assert is_unstable(["CORRECT", "WRONG"]) and not is_unstable(["WRONG", "ERROR", "WRONG"])
    assert agreement(["CORRECT", "WRONG", "CORRECT"]) == 0.667
    assert agreement([]) == 1.0


def test_identical_share_is_the_retrieval_pin() -> None:
    assert identical_share(["k1 | k2", "k1 | k2", "k1 | k2"]) == 1.0
    assert identical_share(["k1 | k2", "k2 | k1", "k1 | k2"]) == 0.667
    assert identical_share([]) == 1.0


# ------------------------------------------------------------------ verdicts


def test_verdict_pass_fail_and_the_two_unresolved_reasons() -> None:
    assert verdict_for(3.0, 1.0, 5.0, band_pp=4.0)[0] == "pass"
    assert verdict_for(-3.0, -5.0, -1.0, band_pp=4.0)[0] == "fail"
    v, reason = verdict_for(-1.18, -3.6, 1.2, band_pp=4.0)
    assert v == "unresolved" and "noise floor" in reason  # the noise-floor gate, inside the band
    v, reason = verdict_for(6.0, -1.0, 13.0, band_pp=4.0)
    assert v == "unresolved" and "under-powered" in reason
    # a non-inferiority threshold: the arm may be up to 0.5 pp worse
    assert verdict_for(-0.2, -0.4, 0.1, band_pp=4.0, min_delta_pp=-0.5)[0] == "pass"


def _labels(n: int, acc: float, seed: int) -> dict[str, list[str]]:
    rng = random.Random(seed)
    return {f"q{i}": ["CORRECT" if rng.random() < acc else "WRONG"] for i in range(n)}


def test_paired_gate_is_paired_and_reads_pass_on_a_real_gain() -> None:
    a = _labels(300, 0.7, seed=1)
    b = {k: (["CORRECT"] if i % 5 == 0 else v) for i, (k, v) in enumerate(a.items())}
    g = paired_gate(a, b, clusters={k: f"c{i % 3}" for i, k in enumerate(a)})
    assert g.n == 300 and g.clusters == 3 and g.losses == 0 and g.wins > 0
    assert g.delta_pp > 0 and g.lb_pp > 0 and g.verdict == "pass"
    assert g.accuracy_b > g.accuracy_a
    assert g.flip_rate_a is None and g.flip_rate_b is None  # single trials


def test_paired_gate_same_labels_is_a_zero_delta_unresolved_inside_the_band() -> None:
    a = _labels(200, 0.75, seed=2)
    g = paired_gate(a, dict(a))
    assert g.delta_pp == 0.0 and g.lb_pp == 0.0 and g.ub_pp == 0.0
    assert g.wins == g.losses == 0 and g.ties == 200
    assert g.verdict == "unresolved" and "noise floor" in g.reason


def test_paired_gate_votes_over_repeats_and_reports_flip_rates() -> None:
    # Arm A: single trials. Arm B: three repeats where a minority run flips
    # 40% of items; the majority keeps B identical to A → zero delta, but
    # the flip rate is visible beside it.
    rng = random.Random(3)
    a = _labels(100, 0.6, seed=3)
    b: dict[str, list[str]] = {}
    for k, (lb,) in a.items():
        other = "WRONG" if lb == "CORRECT" else "CORRECT"
        b[k] = [lb, other, lb] if rng.random() < 0.4 else [lb, lb, lb]
    g = paired_gate(a, b)
    assert g.repeats_a == 1 and g.repeats_b == 3
    assert g.delta_pp == 0.0 and g.ties == 100
    assert g.flip_rate_a is None and g.flip_rate_b is not None and 0.3 < g.flip_rate_b < 0.5
    assert g.discordance == 0.0
    lines = "\n".join(g.lines())
    assert "VERDICT UNRESOLVED" in lines and "flip rate" in lines and "discordance" in lines
    # the stable-only reading excludes the items whose repeats disagreed
    assert g.n_unstable == round(100 * g.flip_rate_b) and g.stable is not None
    assert g.stable["n"] == 100 - g.n_unstable and g.stable["n_unstable"] == 0
    assert "stable items only" in lines


def test_paired_gate_drops_items_whose_majority_errored() -> None:
    a = {"q1": ["CORRECT"], "q2": ["ERROR", "ERROR"], "q3": ["WRONG"]}
    b = {"q1": ["CORRECT"], "q2": ["CORRECT"], "q3": ["CORRECT"], "q9": ["CORRECT"]}
    g = paired_gate(a, b)
    assert g.n == 2 and g.wins == 1 and g.ties == 1


def test_majority_voting_narrows_the_null_band_on_synthetic_reader_noise() -> None:
    """The mechanism the gate rests on: a reader that flips each label with
    probability p per trial gives a same-config pair a band that shrinks
    under a 3-trial majority (per-item flip ≈ 3p² − 2p³ < p)."""
    rng = random.Random(4)
    truth = {f"q{i}": rng.random() < 0.7 for i in range(400)}
    p = 0.085  # the measured single-trial flip rate

    def trial(seed: int, reps: int) -> dict[str, list[str]]:
        r = random.Random(seed)
        out: dict[str, list[str]] = {}
        for k, t in truth.items():
            labels = []
            for _ in range(reps):
                flipped = r.random() < p
                labels.append("CORRECT" if (t != flipped) else "WRONG")
            out[k] = labels
        return out

    g1 = paired_gate(trial(10, 1), trial(11, 1), resamples=2000)
    g3 = paired_gate(trial(12, 3), trial(13, 3), resamples=2000)
    assert band_from_null(g3) < band_from_null(g1)
    # between-arm discordance of the label: single-trial ≈ 2p(1−p) ≈ 15.6%,
    # 3-trial majority ≈ 2q(1−q) with q = 3p² − 2p³ ≈ 2% → ≈ 4%
    assert g1.discordance is not None and g3.discordance is not None
    assert g3.discordance < g1.discordance / 2
    # within-run disagreement is the other reading: 1 − (1−p)³ − p³ ≈ 23%
    assert g3.flip_rate_a is not None and 0.15 < g3.flip_rate_a < 0.32
    assert g1.flip_rate_a is None
    assert g1.verdict == "unresolved" and g3.verdict == "unresolved"


def test_stable_only_pair_removes_coin_flip_items() -> None:
    """An item whose per-trial label is a fair coin stays discordant under any
    vote; the stable-only pair drops it and reports the rest."""
    a = {f"q{i}": ["CORRECT"] * 3 for i in range(50)}
    b = {f"q{i}": ["CORRECT"] * 3 for i in range(50)}
    a["q0"], b["q0"] = ["CORRECT", "WRONG", "CORRECT"], ["WRONG", "CORRECT", "WRONG"]
    a["q1"], b["q1"] = ["WRONG", "WRONG", "WRONG"], ["CORRECT", "CORRECT", "CORRECT"]  # a real move
    g = paired_gate(a, b)
    assert g.discordance == round(2 / 50, 4) and g.n_unstable == 1
    assert (
        g.stable is not None and g.stable["n"] == 49 and g.stable["discordance"] == round(1 / 49, 4)
    )
    assert g.stable["wins"] == 1 and g.stable["losses"] == 0


# ------------------------------------------------------------------ calibration


def test_judge_calibration_de_attenuates_by_youden_and_widens() -> None:
    cal = JudgeCalibration(tpr=0.9, fpr=0.1, n_pos=100, n_neg=100)
    assert cal.youden == pytest.approx(0.8)
    assert cal.correct_accuracy(0.74) == pytest.approx(0.8)
    d, lo, hi = cal.correct_delta(2.0, 0.5, 3.5)
    assert d == pytest.approx(2.5)
    assert hi - lo > 3.5 - 0.5  # the interval carries the calibration variance
    assert lo < d < hi
    # a perfect judge changes nothing but the (tiny) variance term
    perfect = JudgeCalibration(tpr=1.0, fpr=0.0, n_pos=100, n_neg=100)
    assert perfect.correct_delta(2.0, 0.5, 3.5) == pytest.approx((2.0, 0.5, 3.5))
    # a judge with no information leaves the inputs alone
    useless = JudgeCalibration(tpr=0.5, fpr=0.5, n_pos=10, n_neg=10)
    assert useless.correct_delta(2.0, 0.5, 3.5) == (2.0, 0.5, 3.5)


def test_paired_gate_with_calibration_can_turn_a_pass_unresolved() -> None:
    a = _labels(300, 0.7, seed=5)
    b = {k: (["CORRECT"] if i % 6 == 0 else v) for i, (k, v) in enumerate(a.items())}
    plain = paired_gate(a, b)
    weak = paired_gate(a, b, calibration=JudgeCalibration(0.75, 0.25, 20, 20))
    assert plain.verdict == "pass"
    assert weak.calibration is not None and weak.calibration["delta_pp"] > plain.delta_pp
    assert weak.calibration["lb_pp"] < plain.lb_pp
    assert weak.reason.startswith("judge-calibrated")


def test_parse_calibration_inline_and_file(tmp_path) -> None:
    assert parse_calibration("") is None
    c = parse_calibration("0.98,0.03,100,100")
    assert c is not None and (c.tpr, c.fpr, c.n_pos, c.n_neg) == (0.98, 0.03, 100, 100)
    f = tmp_path / "cal.json"
    f.write_text('{"tpr": 1.0, "fpr": 0.01, "n_pos": 100, "n_neg": 100, "judge_model": "j"}')
    c2 = parse_calibration(str(f))
    assert c2 is not None and c2.judge_model == "j" and c2.youden == pytest.approx(0.99)
    with pytest.raises(ValueError):
        parse_calibration("0.9,0.1")


# ------------------------------------------------------------------ CLI glue


def test_add_gate_arguments_and_gate_from_args() -> None:
    p = argparse.ArgumentParser()
    add_gate_arguments(p)
    args = p.parse_args(
        ["--repeats", "3", "--gate", "--gate-band", "2.5", "--gate-min-delta", "-0.5"]
    )
    assert args.repeats == 3 and args.gate and args.gate_band == 2.5 and not args.credit
    a = _labels(120, 0.7, seed=6)
    g = gate_from_args(args, a, dict(a))
    assert g.band_pp == 2.5 and g.min_delta_pp == -0.5 and g.verdict == "pass"
    # a bench with its own --repeats help text keeps it
    q = argparse.ArgumentParser()
    q.add_argument("--repeats", type=int, default=1)
    add_gate_arguments(q, repeats=False)
    assert q.parse_args([]).gate_band == DEFAULT_BAND_PP


def test_labels_by_key_reads_repeat_cells_else_the_label() -> None:
    rows = [
        {
            "sample_id": "c",
            "question_index": "1",
            "label": "CORRECT",
            "repeat_labels": "CORRECT|WRONG|CORRECT",
        },
        {"sample_id": "c", "question_index": "2", "label": "WRONG", "repeat_labels": ""},
    ]
    out = labels_by_key(rows, key=("sample_id", "question_index"))
    assert out[("c", "1")] == ["CORRECT", "WRONG", "CORRECT"] and out[("c", "2")] == ["WRONG"]
    assert labels_by_key(rows, key="question_index")["2"] == ["WRONG"]


def test_measurement_fields_are_the_evaluation_read_unless_credit() -> None:
    assert measurement_fields("search") == SEARCH_MEASUREMENT_FIELDS == {"auto_touch": False}
    assert measurement_fields("memory.ask") == ASK_MEASUREMENT_FIELDS == {"reinforce": False}
    assert measurement_fields("search", credit=True) == {}
    assert measurement_fields("ask", credit=True) == {}
    assert measurement_fields("other") == {}
    # an arm's own override merged after the defaults wins
    assert {**measurement_fields("ask"), **{"reinforce": True}} == {"reinforce": True}


def test_verdict_reads_the_unrounded_interval_and_records_the_resampling(monkeypatch) -> None:
    """An upper bound of -0.004 pp is below zero: rounding it to -0.00 first would
    have read the interval as straddling the threshold."""
    import bench._gate as g
    from bench.bootstrap import BootResult

    monkeypatch.setattr(
        g,
        "paired_bootstrap",
        lambda deltas, cl, resamples, seed: BootResult(
            len(deltas), len(deltas), -5e-5, -6e-5, -4e-5, 0, len(deltas) - 1, 1, resamples
        ),
    )
    a = {f"q{i}": ["CORRECT"] for i in range(10)}
    b = {**a, "q0": ["WRONG"]}
    r = g.paired_gate(a, b, resamples=500, seed=11)
    assert r.verdict == "fail" and r.ub_pp == -0.0
    assert r.resamples == 500 and r.seed == 11
    assert r.as_dict()["seed"] == 11
