"""Separate the question from the answer contract around it.

Callers wrap questions in scaffolding the retriever should never see: a
block of lettered options, "answer with the letter only", "Current date:
2024-05-01", "respond in JSON". That text is for the synthesizer. Embedded
with the question it drags the top-hit similarity of a multiple-choice ask
from 0.42–0.47 to 0.21–0.37 on the same memories (multiple-choice participation
items) and trips the abstain gate with the evidence in hand.

`split_answer_scaffold` returns (stem, scaffold): the stem is what gets
embedded and parsed for intent; the full text still goes to the model.
Deterministic and conservative: a query with no recognisable scaffold is
returned unchanged, and a stem that would be too short to retrieve on is
not split.
"""

from __future__ import annotations

import re

# A lettered option line: "A) 6 people", "(B) ...", "C. ...", "D: ...", and
# the unspaced "A.Akib ordered food" form (some dialogue datasets write their
# options that way; unsplit, a 466-character question falls under the noise floor and
# memory.ask abstains). A lowercase letter still needs the
# space, so "e.g." and "a.m." at a line start stay prose.
_OPTION_LINE_RE = re.compile(r"^\s*\(?(?:([A-H])[\)\.:]\s*|([a-h])[\)\.:]\s+)\S")
# Instruction lines that describe the answer format, not the question.
_FORMAT_LINE_RE = re.compile(
    r"^\s*("
    r"(choose|select|pick|answer|reply|respond)\b.*\b(letter|option|options|choice|choices)\b"
    r"|answer with\b|reply with\b|respond (only )?with\b|respond in\b|reply in\b"
    r"|(options|choices|answer choices)\s*:\s*$"
    r"|(return|output|format)\b.*\b(json|letter|one word|single word|number only)\b"
    r")",
    re.IGNORECASE,
)
# A date the caller stamps for relative-time resolution ("Current date: …").
_DATE_LINE_RE = re.compile(
    r"^\s*(current date|question date|today('s date)?|date|as of)\s*:\s*\S+.*$",
    re.IGNORECASE,
)
_MIN_STEM_CHARS = 12
_MIN_OPTION_LINES = 2


def split_answer_scaffold(query: str) -> tuple[str, str]:
    """(stem, scaffold). `scaffold` is empty when nothing was stripped."""
    text = (query or "").strip()
    if not text or "\n" not in text:
        return text, ""
    lines = text.split("\n")
    keep: list[str] = []
    dropped: list[str] = []
    option_lines = 0
    for line in lines:
        if not line.strip():
            keep.append(line)
            continue
        if _OPTION_LINE_RE.match(line):
            option_lines += 1
            dropped.append(line)
        elif _FORMAT_LINE_RE.match(line) or _DATE_LINE_RE.match(line):
            dropped.append(line)
        else:
            keep.append(line)
    # A single lettered line is more likely prose ("A) is my favourite") than
    # an option block; put it back.
    if 0 < option_lines < _MIN_OPTION_LINES:
        return text, ""
    if not dropped:
        return text, ""
    stem = re.sub(r"\n{2,}", "\n", "\n".join(keep)).strip()
    if len(stem) < _MIN_STEM_CHARS:
        return text, ""
    return stem, "\n".join(dropped).strip()


__all__ = ["split_answer_scaffold"]
