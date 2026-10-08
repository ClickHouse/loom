"""The premise verdict rides the synthesis call."""

from __future__ import annotations

from server.memory.domain import premise_line as pl


def test_split_premise_reads_both_verdicts_and_strips_the_line() -> None:
    body, v, s = pl.split_premise("Premise: supported\n\nRunning. [melanie_running_shoes]")
    assert (body, v, s) == ("Running. [melanie_running_shoes]", "supported", "")
    body, v, s = pl.split_premise(
        "Premise: unsupported — The memories do not record Caroline getting new shoes.\n\n"
        "Melanie, not Caroline, got pink running shoes. [melanie_running_shoes]"
    )
    assert v == "unsupported" and s == "The memories do not record Caroline getting new shoes."
    assert body.startswith("Melanie, not Caroline")
    # bold, colon variants and a missing dash still parse
    assert (
        pl.split_premise("**Premise:** unsupported: nothing about Nate's turtles\nX")[1]
        == "unsupported"
    )
    assert pl.split_premise("premise: SUPPORTED\nX")[1] == "supported"
    # no line → nothing stripped, verdict None
    assert pl.split_premise("Running. [k]") == ("Running. [k]", None, "")
    assert pl.split_premise("") == ("", None, "")


def test_unsupported_moves_the_models_refusal_sentence_first() -> None:
    ans = "Melanie, not Caroline, got pink running shoes for running. [melanie_running_shoes]"
    out, reason = pl.apply(
        ans, "unsupported", "The memories do not record Caroline getting new shoes"
    )
    assert reason == "premise_unsupported"
    assert out == (
        "The memories do not record Caroline getting new shoes. "
        "Melanie, not Caroline, got pink running shoes for running. [melanie_running_shoes]"
    )


def test_a_sentence_that_is_not_a_refusal_falls_back_to_the_generic_one() -> None:
    out, reason = pl.apply("Running. [k]", "unsupported", "Caroline likes running")
    assert reason == "premise_unsupported" and out.startswith(pl.GENERIC_REFUSAL)
    out, _ = pl.apply("Running. [k]", "unsupported", "")
    assert out.startswith(pl.GENERIC_REFUSAL)


def test_an_answer_already_refusing_is_left_alone_and_supported_is_untouched() -> None:
    ans = "The memories do not record Caroline buying shoes. Melanie did. [k]"
    assert pl.apply(ans, "unsupported", "The memories do not record Caroline buying shoes") == (
        ans,
        "premise_unsupported:already_first",
    )
    assert pl.apply("Running. [k]", "supported", "") == ("Running. [k]", "")
    assert pl.apply("Running. [k]", None, "") == ("Running. [k]", "")


def test_instruction_opens_the_reply() -> None:
    assert pl.instruction().lstrip().startswith("FIRST LINE OF YOUR REPLY")


def test_explicit_form_reads_the_clause_then_the_verdict() -> None:
    body, v, s = pl.split_premise(
        "Premise: Caroline got new shoes — unsupported — The memories do not record Caroline getting shoes.\n\n"
        "Melanie, not Caroline, got pink running shoes. [melanie_running_shoes]"
    )
    assert v == "unsupported" and s == "The memories do not record Caroline getting shoes."
    assert body.startswith("Melanie, not Caroline")
    body, v, s = pl.split_premise(
        "Premise: Melanie's new shoes have a use — supported\n\nRunning. [k]"
    )
    assert (v, s, body) == ("supported", "", "Running. [k]")
    # a clause containing a dash still parses on the LAST verdict token
    body, v, s = pl.split_premise("**Premise:** Nate owns a console - supported\nA Switch. [k]")
    assert v == "supported" and body == "A Switch. [k]"
    assert "WHO did, has or said WHAT" in pl.instruction()
    assert "LIKELY" not in pl.instruction()  # forbid posture: a refused inference is the posture
    assert "LIKELY" in pl.instruction(infer_ok=True)  # allow posture: inference is an answer


def test_explicit_line_carries_no_speaker_rule() -> None:
    assert "said by" not in pl.instruction()
    assert "said by" not in pl.instruction(infer_ok=True)
