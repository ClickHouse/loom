"""The second-judge sample is deterministic and the agreement block reads it."""

from __future__ import annotations

from dataclasses import fields

from bench.locomo_qa import QuestionResult, judge_agreement_summary, second_judge_sample


def _r(sample_id: str, idx: int, label: str, second: str = "", cat: int = 1) -> QuestionResult:
    from dataclasses import MISSING

    base = dict(
        sample_id=sample_id,
        namespace="ns",
        question_index=idx,
        category=cat,
        category_name={1: "multi_hop", 2: "temporal", 5: "adversarial"}.get(cat, "single_hop"),
        question="q",
        gold="g",
        generated="a",
        label=label,
        judge_reply="",
        abstained=False,
    )
    # every other required field gets a typed zero so the record constructs
    for f in fields(QuestionResult):
        if f.name in base or f.default is not MISSING or f.default_factory is not MISSING:
            continue
        base[f.name] = (
            {int: 0, float: 0.0, bool: False, str: ""}.get(f.type, "")
            if isinstance(f.type, type)
            else ("" if "str" in str(f.type) else 0)
        )
    r = QuestionResult(**base)
    r.second_judge_label = second
    return r


def test_sample_is_even_deterministic_and_skips_errors() -> None:
    rows = [_r("c1", i, "CORRECT" if i % 2 else "WRONG") for i in range(10)] + [
        _r("c0", 0, "ERROR")
    ]
    s1 = second_judge_sample(rows, 4)
    s2 = second_judge_sample(list(reversed(rows)), 4)
    assert [(r.sample_id, r.question_index) for r in s1] == [
        (r.sample_id, r.question_index) for r in s2
    ]
    assert len(s1) == 4 and all(r.label != "ERROR" for r in s1)
    assert len(second_judge_sample(rows, 100)) == 10
    assert second_judge_sample(rows, 0) == []


def test_agreement_summary_counts_disagreements_by_category() -> None:
    rows = [
        _r("c", 0, "CORRECT", "CORRECT"),
        _r("c", 1, "WRONG", "CORRECT", cat=2),
        _r("c", 2, "WRONG", "WRONG"),
        _r("c", 3, "CORRECT", "WRONG", cat=2),
        _r("c", 4, "CORRECT", ""),  # not sampled
    ]
    ja = judge_agreement_summary(rows, "gpt-x")
    assert ja["sampled"] == 4 and ja["agreement"] == 0.5 and ja["disagreements"] == 2
    assert ja["disagreements_by_category"] == {"temporal": 2}
    assert ja["kappa"] == 0.0
    assert ja["second_judge_accuracy"] == 50.0 and ja["first_judge_accuracy_on_sample"] == 50.0
