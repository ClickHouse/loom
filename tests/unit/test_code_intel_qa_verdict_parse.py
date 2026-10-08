"""The judge lane keeps the rationale whether it follows the verdict on the same line or the next."""

from bench.code_intel_qa import parse_verdict


def test_same_line_rationale_is_kept() -> None:
    assert parse_verdict("REJECT. The answer cites the wrong file.") == (
        "reject",
        "The answer cites the wrong file.",
    )
    assert parse_verdict("ACCEPT: grounded and consistent.") == (
        "accept",
        "grounded and consistent.",
    )


def test_next_line_rationale_and_markdown() -> None:
    assert parse_verdict("ACCEPT\nWell grounded.\nCites the mechanism.") == (
        "accept",
        "Well grounded. Cites the mechanism.",
    )
    assert parse_verdict("**REJECT**\nUngrounded.")[0] == "reject"
    assert parse_verdict("") == ("reject", "")
