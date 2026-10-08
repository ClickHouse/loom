"""The premise verdict rides the synthesis call.

When a question presupposes a person, event or attribute no memory records, the
refusal has to come first: on conversation QA the judge accepted every answer that opened
with a refusal (even when it went on to state a related fact) and failed most of
those that opened with an assertion. A prose rule asking for that order moved the
correction first only 41 percent of the time.

This module makes the verdict a structured line the model must write before
the answer, the same way the model declares its evidence:
``Premise: supported`` or ``Premise: unsupported — <one sentence saying what
no memory records>``. The line is stripped from the answer; when it says
unsupported and the answer does not already open with a refusal, the
model's own sentence is moved to the front. The model still writes the
whole answer; code only guarantees the order the reader relies on.
"""

from __future__ import annotations

import contextvars
import re

# The verdict of the most recent synthesis call: "supported", "unsupported",
# or None when the call did not run with the line on or the line did not
# parse. Read by ask() right after each _synthesize call.
LAST_PREMISE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "loom_ask_last_premise", default=None
)

_PREMISE_LINE_RE = re.compile(
    r"^\s*\**\s*premise\s*\**\s*:\s*\**\s*(supported|unsupported)\s*\**\s*(?:[—:\-–]+\s*(.*))?$",
    re.IGNORECASE,
)
# The explicit form (models find a presupposition far more reliably than they
# judge it implicitly): the model first writes what
# the question takes as given, then the verdict, then the refusal sentence.
# `Premise: Priya got new shoes — unsupported — The memories do not record …`
_PREMISE_EXPLICIT_RE = re.compile(
    r"^\s*\**\s*premise\s*\**\s*:\s*(.+?)\s*[—:\-–]+\s*\**\s*(supported|unsupported)\s*\**\s*(?:[—:\-–]+\s*(.*))?$",
    re.IGNORECASE,
)

# An answer that already opens with a refusal needs no reordering.
REFUSAL_OPEN_RE = re.compile(
    r"^\W*(?:the\s+)?(?:memories|memory|records?|store)\s+(?:do|does)\s+not\s+"
    r"(?:record|mention|say|contain|show|include|have)"
    r"|^\W*(?:there\s+is\s+)?no\s+(?:memory|record)"
    r"|^\W*i\s+(?:do\s+not|don't)\s+have\s+(?:enough\s+)?(?:information|memories)"
    r"|^\W*(?:it\s+is\s+)?not\s+recorded",
    re.IGNORECASE,
)

GENERIC_REFUSAL = "The memories do not record what the question takes as given."


def instruction(infer_ok: bool = False) -> str:
    return (
        " FIRST LINE OF YOUR REPLY: `Premise: <what the question takes as given — WHO did, "
        "has or said WHAT> — supported` when the memories record exactly that person with "
        "that event or attribute; otherwise `Premise: <the same clause> — "
        "unsupported — ` followed by ONE sentence that starts with 'The memories do "
        "not record' and names what is missing. Write the clause before you judge "
        "it. "
        + (
            "A question that asks what someone would LIKELY do, be or prefer, or "
            "that asks for a judgement from what is recorded, presupposes only that "
            "the memories bear on it: mark it `supported` when they do and answer "
            "with the inference. "
            if infer_ok
            else ""
        )
        + "Then a blank line, then the answer. Under `unsupported`, the answer "
        "may state a related memory about someone or something else only if it "
        "names the difference. Do not repeat the Premise line anywhere else."
    )


def split_premise(text: str) -> tuple[str, str | None, str]:
    """Strip a leading ``Premise:`` line. Returns (answer, verdict, sentence):
    verdict is "supported" / "unsupported" or None when no line is present;
    sentence is the text after the dash on an unsupported line, or ""."""
    if not text:
        return text, None, ""
    lines = text.split("\n")
    m = _PREMISE_LINE_RE.match(lines[0])
    if m:
        verdict = m.group(1).lower()
        sentence = (m.group(2) or "").strip().strip("*").strip()
    else:
        m2 = _PREMISE_EXPLICIT_RE.match(lines[0])
        if not m2:
            return text, None, ""
        verdict = m2.group(2).lower()
        sentence = (m2.group(3) or "").strip().strip("*").strip()
    rest = "\n".join(lines[1:]).lstrip("\n").strip()
    return rest, verdict, sentence


def _as_refusal(sentence: str) -> str:
    s = (sentence or "").strip()
    if not s:
        return GENERIC_REFUSAL
    if not REFUSAL_OPEN_RE.search(s):
        return GENERIC_REFUSAL
    if s[-1] not in ".!?":
        s += "."
    return s


def apply(answer: str, verdict: str | None, sentence: str) -> tuple[str, str]:
    """Reorder so the refusal comes first when the model declared the premise
    unsupported. Returns (answer, reason): reason is "" when nothing moved,
    "premise_unsupported" when the refusal sentence was put in front, and
    "premise_unsupported:already_first" when the answer already opened with
    one. A supported or missing verdict leaves the answer alone."""
    text = (answer or "").strip()
    if verdict != "unsupported":
        return answer, ""
    if not text:
        return _as_refusal(sentence), "premise_unsupported"
    if REFUSAL_OPEN_RE.search(text):
        return answer, "premise_unsupported:already_first"
    return f"{_as_refusal(sentence)} {text}", "premise_unsupported"
