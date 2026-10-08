"""The program computes what the question asks to be computed."""

from __future__ import annotations

from server.memory.domain.ask_arithmetic import (
    verify_arithmetic,
    verify_digit_sum,
    verify_name_length,
)

SCAFFOLD = "Current date: 2024-10-05\n{stem}\n\nChoose exactly one option and reply with its letter only.\n{opts}"


def _q(stem: str, opts: str) -> str:
    return SCAFFOLD.format(stem=stem, opts=opts)


def test_letters_in_a_name_exclude_the_space() -> None:
    q = _q(
        "How many letters are in the name of a person whose birthday falls on October 25th?",
        "A) 12 characters\nB) 14 characters\nC) 15 characters\nD) 13 characters",
    )
    a = 'B) 14 characters — "Isaac Thornton" (Oct 25th) has 14 letters counting the space. [isaac_thornton_birthday]'
    r = verify_name_length(a, q)
    assert (
        r.changed
        and r.letter == "D"
        and r.text.startswith("D) 13 characters — computed: 'Isaac Thornton' has 13 letters")
    )
    # agrees → untouched; two names → abstains
    assert verify_name_length("D) 13 characters — Isaac Thornton", q).changed is False
    assert verify_name_length("B) 14 — Isaac Thornton or Lila Brooks", q).changed is False


def test_digit_sum_from_the_stated_number() -> None:
    q = _q(
        "What is the sum of the last six digits of the contact number for the person who works as a farmer?",
        "A) 33\nB) 30\nC) 31\nD) 36",
    )
    a = "C) 31 — the farmer is Mason Reed, contact 202-555-79840; 5+7+9+8+4 = 31 [mason_reed_phone]"
    r = verify_digit_sum(a, q)
    assert r.changed and r.letter == "A" and "579840" in r.detail and "sum 33" in r.detail
    assert verify_arithmetic(a, q).letter == "A"
    # no number stated → untouched
    assert verify_digit_sum("C) 31 — no contact number in memory", q).changed is False


def test_other_questions_are_left_alone() -> None:
    q = _q("What is the position of my father?", "A) Corporate Accountant\nB) Corporate Attorney")
    assert (
        verify_arithmetic("B) Corporate Attorney — Nolan Pierce works as one", q).changed is False
    )
