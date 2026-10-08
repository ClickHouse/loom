"""The literal values a count question names (quoted strings, capitalised spans)."""

from __future__ import annotations

import re

_QUOTED_RE = re.compile(r"[\"\u201c\u2018\'](.{2,60}?)[\"\u201d\u2019\']")
_CAP_SPAN_RE = re.compile(r"\b([A-Z][a-zA-Z\'&.-]+(?:\s+[A-Z][a-zA-Z\'&.-]+){0,3})\b")
_STOP = {
    "how",
    "many",
    "what",
    "which",
    "who",
    "when",
    "where",
    "additionally",
    "also",
    "current",
    "date",
    "choose",
    "reply",
    "the",
    "in",
    "my",
    "i",
    "a",
    "an",
    "and",
    "do",
    "does",
    "did",
    "are",
    "is",
    "was",
    "were",
    "have",
    "has",
    "had",
    "people",
    "person",
    "events",
    "event",
    "items",
    "item",
    "list",
    "options",
    "option",
}
_US_STATE_TAIL_RE = re.compile(r",?\s+[A-Z]{2}$")


def question_literals(stem: str, *, max_literals: int = 2) -> list[str]:
    """The literal values a count question names: quoted strings first, then
    capitalised spans that are not question words. A trailing US state code
    ("Chicago, IL") is dropped so the literal matches "in Chicago" too."""
    from server.memory.domain.ask_option_guard import question_stem

    text = question_stem(stem)
    out: list[str] = []
    for m in _QUOTED_RE.finditer(text):
        lit = m.group(1).strip()
        if lit and lit.lower() not in _STOP:
            out.append(lit)
    for m in _CAP_SPAN_RE.finditer(text):
        span = _US_STATE_TAIL_RE.sub("", m.group(1)).strip()
        words = [w for w in span.split() if w.lower() not in _STOP]
        if not words:
            continue
        span = " ".join(words)
        if len(span) < 3 or span.lower() in _STOP or span in out:
            continue
        out.append(span)
    return out[:max_literals]
