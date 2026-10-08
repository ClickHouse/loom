"""Retrieval sees the question, synthesis sees the contract.

Four MemBench questions score top-hit similarity 0.42–0.47 as bare stems and
0.21–0.37 with the option block and
"reply with its letter only" appended — under the 0.27 abstain gate, with
the evidence in the results. CH-free.
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.memory.domain.query_scaffold import split_answer_scaffold

_MC = (
    "Current date: 2024-05-01\n"
    "How many individuals are 28 years old or older?\n\n"
    "Choose exactly one option and reply with its letter only.\n"
    "A) 6 people\nB) 5 people\nC) 7 people\nD) 8 people"
)


def test_multiple_choice_query_splits_to_the_question() -> None:
    stem, scaffold = split_answer_scaffold(_MC)
    assert stem == "How many individuals are 28 years old or older?"
    assert "A) 6 people" in scaffold and "letter only" in scaffold
    assert "Current date" in scaffold


def test_plain_question_is_unchanged() -> None:
    for q in (
        "How long did FinSight 2024 last?",
        "what did we decide about the deploy schedule\nand who owns it",
        "",
    ):
        assert split_answer_scaffold(q) == (q.strip(), "")


def test_one_lettered_line_is_prose_not_an_option_block() -> None:
    q = "Which plan did I pick?\nA) was the one with the free tier, I think"
    assert split_answer_scaffold(q) == (q, "")


def test_dot_and_paren_option_styles_and_json_instruction() -> None:
    q = "Where does Maya live now?\n(A) Austin\n(B) Lisbon\n(C) Berlin\nRespond in JSON with a single key answer."
    stem, scaffold = split_answer_scaffold(q)
    assert stem == "Where does Maya live now?"
    assert "(C) Berlin" in scaffold and "JSON" in scaffold
    q2 = "Which city did we pick?\nA. Austin\nB. Lisbon"
    assert split_answer_scaffold(q2)[0] == "Which city did we pick?"


def test_too_short_stem_is_not_split() -> None:
    q = "Which?\nA) x\nB) y"
    assert split_answer_scaffold(q) == (q, "")


def test_handler_retrieves_on_the_stem_and_answers_the_full_text(monkeypatch) -> None:
    import server.memory.app.ask.handler as ha
    from server.memory.api.models_ask import MemoryAskInput

    captured: dict[str, Any] = {}

    async def _fake_ask(search_input, *, question=None, **kw):
        captured["retrieval"] = search_input.query
        captured["question"] = question
        from server.memory.app.ask import MemoryAskResult

        return MemoryAskResult(answer="B", memory_keys=["k"])

    async def _zero(_input):
        return 0

    monkeypatch.setattr("server.memory.app.ask.ask", _fake_ask)
    monkeypatch.setattr(ha, "_pending_extraction", _zero)

    out = asyncio.run(
        ha.handle_memory_ask(
            MemoryAskInput(org="o", namespace="n", agent="*", query=_MC, allow_synthesis=False)
        )
    )
    assert captured["retrieval"] == "How many individuals are 28 years old or older?"
    assert captured["question"] == _MC
    assert out.retrieval_query == "How many individuals are 28 years old or older?"

    captured.clear()
    out = asyncio.run(
        ha.handle_memory_ask(
            MemoryAskInput(
                org="o",
                namespace="n",
                agent="*",
                query="plain question here",
                allow_synthesis=False,
            )
        )
    )
    assert captured["retrieval"] == "plain question here"
    assert out.retrieval_query == ""


def test_unspaced_uppercase_options_are_scaffold_but_lowercase_abbreviations_are_prose():
    from server.memory.domain.query_scaffold import split_answer_scaffold

    q = (
        "What was the third activity Matt did with his new basketball?\n"
        "A.Playing basketball with his team.  \nB.Playing with his friends.\n"
        "C.Participating in a tournament.  \nD.Shooting hoops at a park"
    )
    stem, scaffold = split_answer_scaffold(q)
    assert stem == "What was the third activity Matt did with his new basketball?"
    assert scaffold.startswith("A.Playing") and scaffold.count("\n") == 3
    prose = "Notes on the plan\ne.g. this line is prose\na.m. is the morning\nnext"
    assert split_answer_scaffold(prose) == (prose, "")
