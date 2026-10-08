"""The program computes what the question asks to be computed.

Post-processing multiple-choice items ask for a computation over a retrieved
value: "how many letters are in the name of the person whose birthday is
October 25th", "the sum of the last six digits of the contact number of the
person who works as a farmer". The reader finds the person and then counts
or adds in prose: "Isaac Thornton" counted as 14 "counting the
space" against 13 letters. The name and the number the answer states are
the inputs; the count and the sum are arithmetic, and arithmetic is not the
model's job. Runs only when the stem asks for that computation, only when
the answer states exactly one candidate input, and never overrides an
answer it agrees with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from server.memory.domain.ask_option_guard import parse_options, question_stem

_LEAD_RE = re.compile(r"^\s*\(?([A-Ha-h])[\)\.:]\s*")
_LETTERS_Q_RE = re.compile(r"\bhow many (letters|characters)\b.*\bname\b", re.I)
_DIGIT_SUM_Q_RE = re.compile(r"\bsum of (?:the )?(first|last)\s+(\w+)\s+digits\b", re.I)
_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:[ '\-][A-Z][a-z]+)+)\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s().]{6,}\d)(?!\d)")
_INT_RE = re.compile(r"\d+")
_WORD_NUM = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
# Words that look like names but are not the person the question is about.
_NOT_NAMES = frozenset(
    {
        "October",
        "November",
        "December",
        "January",
        "February",
        "March",
        "April",
        "June",
        "July",
        "August",
        "September",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "Miami",
        "Chicago",
        "Boston",
    }
)


@dataclass(frozen=True)
class Computed:
    text: str
    changed: bool = False
    letter: str = ""
    detail: str = ""


def _option_for_int(options: dict[str, str], value: int) -> str:
    hits = [k for k, v in options.items() if (m := _INT_RE.search(v)) and int(m.group(0)) == value]
    return hits[0] if len(hits) == 1 else ""


def _rewrite(answer: str, options: dict[str, str], letter: str, detail: str) -> Computed:
    first, sep, rest = answer.partition("\n")
    lead = _LEAD_RE.match(first)
    chosen = lead.group(1).upper() if lead else ""
    if chosen == letter:
        return Computed(answer, letter=letter, detail=detail)
    body = first[lead.end() :].strip() if lead else first.strip()
    new_first = f"{letter}) {options[letter]} — computed: {detail}. Reader: {body}"
    return Computed(
        new_first + (sep + rest if sep else ""), changed=True, letter=letter, detail=detail
    )


def names_in(answer: str, options: dict[str, str]) -> list[str]:
    """Distinct multi-word capitalised spans in the answer that are not an
    option's text or a month/weekday/city: the person the answer names."""
    opts = {v.strip().lower() for v in options.values()}
    out: list[str] = []
    for m in _NAME_RE.finditer(answer or ""):
        name = m.group(1)
        if name.lower() in opts or any(w in _NOT_NAMES for w in re.split(r"[ '\-]", name)):
            continue
        if name not in out:
            out.append(name)
    return out


def verify_name_length(answer: str, question: str) -> Computed:
    """ "How many letters are in the name of …": letters are the alphabetic
    characters of the name the answer states — a space is not a letter."""
    stem = question_stem(question)
    options = parse_options(question)
    if not options or not answer or not _LETTERS_Q_RE.search(stem):
        return Computed(answer)
    names = names_in(answer, options)
    if len(names) != 1:
        return Computed(answer)
    n = sum(1 for ch in names[0] if ch.isalpha())
    letter = _option_for_int(options, n)
    if not letter:
        return Computed(answer)
    return _rewrite(answer, options, letter, f"{names[0]!r} has {n} letters")


def verify_digit_sum(answer: str, question: str) -> Computed:
    """ "The sum of the last six digits of the contact number …": summed from
    the one phone-shaped number the answer states."""
    stem = question_stem(question)
    options = parse_options(question)
    m = _DIGIT_SUM_Q_RE.search(stem) if options and answer else None
    if not m:
        return Computed(answer)
    which, count_word = m.group(1).lower(), m.group(2).lower()
    count = _WORD_NUM.get(count_word) or (int(count_word) if count_word.isdigit() else 0)
    if not count:
        return Computed(answer)
    numbers = []
    for pm in _PHONE_RE.finditer(answer):
        digits = re.sub(r"\D", "", pm.group(0))
        if len(digits) >= max(7, count) and digits not in numbers:
            numbers.append(digits)
    if len(numbers) != 1:
        return Computed(answer)
    digits = numbers[0]
    picked = digits[-count:] if which == "last" else digits[:count]
    total = sum(int(d) for d in picked)
    letter = _option_for_int(options, total)
    if not letter:
        return Computed(answer)
    return _rewrite(
        answer,
        options,
        letter,
        f"{which} {count} digits of {digits} are {picked}, sum {total}",
    )


def verify_arithmetic(answer: str, question: str) -> Computed:
    r = verify_name_length(answer, question)
    if r.changed or r.letter:
        return r
    return verify_digit_sum(answer, question)


__all__ = ["Computed", "names_in", "verify_arithmetic", "verify_digit_sum", "verify_name_length"]
