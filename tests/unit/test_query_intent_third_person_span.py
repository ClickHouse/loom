"""Third-person span questions reach the date-difference leg."""

from __future__ import annotations

import pytest

from server.memory.domain.query_intent import parse_query_intent


def _ops(q: str) -> list[str] | None:
    it = parse_query_intent(q)
    spec = it.temporal_diff_spec() if it is not None else None
    return list(spec["operands"]) if spec else None


@pytest.mark.parametrize(
    ("question", "operands"),
    [
        (
            "How many weeks passed between Maria adopting Coco and Shadow?",
            ["maria adopting coco", "maria adopting shadow"],
        ),
        (
            "How many months lapsed between Sam's first and second doctor's appointment?",
            ["sam's first doctor's appointment", "second doctor's appointment"],
        ),
        (
            "How long did it take for Joanna to finish writing her book?",
            ["joanna began writing her book", "joanna finished writing her book"],
        ),
        (
            "How long did Evan and his partner date before getting married?",
            ["evan and his partner date", "getting married"],
        ),
        (
            "How long did Dave's work on the Ford Mustang take?",
            ["dave's work on the ford mustang started", "dave's work on the ford mustang ended"],
        ),
        (
            "How long was the car modification workshop in San Francisco?",
            [
                "the car modification workshop in san francisco started",
                "the car modification workshop in san francisco ended",
            ],
        ),
    ],
)
def test_third_person_span_shapes_produce_two_probes(question: str, operands: list[str]) -> None:
    it = parse_query_intent(question)
    assert it is not None and it.query_shape == "temporal_diff" and it.op == "date_span"
    assert _ops(question) == operands


def test_first_person_shapes_still_work_and_plain_questions_do_not_fire() -> None:
    assert _ops(
        "How many days between when I launched my website and when I got my first customer?"
    )
    assert _ops("How long did it take for Joanna to finish writing her book?") is not None
    assert _ops("When did Melanie paint a sunrise?") is None
    assert _ops("How long has Caroline had her current group of friends for?") is None
    assert _ops("What is the capital of France?") is None
