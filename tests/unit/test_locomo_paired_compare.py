"""bench.locomo_qa.paired_compare: pairing by question text across fresh
namespaces, the exact sign test, per-category counts, same-context flips."""

from __future__ import annotations

from bench.locomo_qa import paired_compare, sign_test_p


def _row(sample, idx, q, label, cat="multi_hop", ctx="100"):
    return {
        "sample_id": sample,
        "question_index": str(idx),
        "question": q,
        "label": label,
        "category_name": cat,
        "context_tokens": ctx,
        "policy": "default",
    }


def test_sign_test_is_exact_and_two_sided() -> None:
    assert sign_test_p(0, 0) == 1.0
    assert sign_test_p(3, 3) == 1.0
    assert abs(sign_test_p(14, 1) - 0.00098) < 0.0002  # a 14-vs-1 flip split
    assert abs(sign_test_p(14, 5) - 0.0636) < 0.001
    assert abs(sign_test_p(14, 9) - 0.405) < 0.002
    assert sign_test_p(5, 0) == sign_test_p(0, 5)


def test_pairs_by_question_text_when_sample_ids_differ() -> None:
    a = [_row("ns-off", i, f"q{i}", "CORRECT" if i < 6 else "WRONG") for i in range(10)]
    b = [_row("ns-on", i, f"q{i}", "CORRECT" if i < 8 else "WRONG") for i in range(10)]
    r = paired_compare(a, b)
    assert r["paired_questions"] == 10 and r["paired_by"] == "question_text"
    assert (r["up"], r["down"]) == (2, 0)
    assert r["accuracy_a"] == 0.6 and r["accuracy_b"] == 0.8
    assert r["sign_test_p"] == 0.5
    assert r["by_category"]["multi_hop"] == {"n": 10, "a": 6, "b": 8, "up": 2, "down": 0}


def test_same_context_flips_and_categories() -> None:
    a = [
        _row("s", 0, "q0", "CORRECT", "temporal", "900"),
        _row("s", 1, "q1", "WRONG", "temporal", "900"),
        _row("s", 2, "q2", "CORRECT", "single_hop", "500"),
    ]
    b = [
        _row(
            "s", 0, "q0", "WRONG", "temporal", "900"
        ),  # flipped, same context → synthesizer re-roll
        _row("s", 1, "q1", "CORRECT", "temporal", "1200"),  # flipped, retrieval changed
        _row("s", 2, "q2", "CORRECT", "single_hop", "500"),
    ]
    r = paired_compare(a, b)
    assert r["paired_by"] == "sample_id"
    assert r["same_context_flips"] == 1 and (r["up"], r["down"]) == (1, 1)
    assert r["by_category"]["temporal"]["up"] == 1 and r["by_category"]["single_hop"]["n"] == 1
