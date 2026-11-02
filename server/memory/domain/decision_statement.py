"""A decision as one statement for its memory row: what search embeds and
memory.for_file shows. Each part is clipped so the statement fits the embedded excerpt."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

# Caps counted JSON-encoded, so the longest statement fits the excerpt less its key
# suffix, and memory.for_file's line reaches the outcome.
STATEMENT_SCENARIO_CHARS = 80
STATEMENT_OUTCOME_CHARS = 200
STATEMENT_REASONING_CHARS = 360
STATEMENT_REJECTED_CHARS = 240
STATEMENT_REALIZED_CHARS = 170
_ELLIPSIS = "..."


# What a part ends without: its own full stop, comma, semicolon or colon, ASCII or
# full-width, would double the statement's punctuation.
_PART_ENDINGS = " .,;:。，、；："
# What a cut drops before its ellipsis: those, and the dashes and slashes it may leave.
_CUT_ENDINGS = _PART_ENDINGS + "-—/"
# A part that ends a sentence itself (a question, an exclamation, a trailing ellipsis)
# keeps its mark.
_SENTENCE_ENDS = ".!?？！…"
# A reason written as "Because …": the statement says "because" itself.
_LEADING_BECAUSE = re.compile(r"^because\b[ ,:;.…—-]*", re.IGNORECASE)


def collapse_ws(text: Any) -> str:
    """`text` on one line, each run of whitespace a single space."""
    return " ".join(str(text or "").split())


def _encoded_len(text: str) -> int:
    """`text`'s length as memory.set stores it, JSON-encoded, without the quotes."""
    return len(json.dumps(text)) - 2


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _hangul(ch: str) -> bool:
    return unicodedata.name(ch, "").startswith("HANGUL")


def _wide_pair(a: str, b: str) -> bool:
    """Whether a cut may fall between `a` and `b`: two wide characters, unless both are
    Hangul (Korean spaces its words)."""
    return _wide(a) and _wide(b) and not (_hangul(a) and _hangul(b))


def _wide_cut(text: str, end: int, cut: int) -> int:
    """The latest point after `cut`, up to `end`, between two wide characters; else `cut`."""
    return next((i for i in range(end, cut, -1) if _wide_pair(text[i - 1], text[i])), cut)


def _clip(text: str, limit: int) -> str:
    """`text` cut to `limit` JSON-encoded characters, ending in an ellipsis when cut.

    The cut falls on the last space, so no half token (`guide.md` of `guide.mdx`) reads
    as a file of its own. Wide (CJK) text may be cut between two wide characters instead,
    when that is later, but never between two Hangul syllables (Korean spaces its words,
    so it is cut at a space). With neither before the limit nothing is kept.
    """
    if _encoded_len(text) <= limit:
        return text
    used = 0
    end = 0
    for ch in text:
        used += _encoded_len(ch)
        if used > limit - len(_ELLIPSIS):
            break
        end += 1
    last_space = max(0, text.rfind(" ", 0, end + 1))
    cut = _wide_cut(text, end, last_space)
    return text[:cut].rstrip(_CUT_ENDINGS) + _ELLIPSIS


def _sentence(text: str) -> str:
    """`text` ending in a full stop, unless it already ends a sentence."""
    return text if not text or text[-1] in _SENTENCE_ENDS else text + "."


def alternatives(payload: dict[str, Any]) -> list[str]:
    """The rejected options of a payload's list or a stored record's lines, blanks dropped."""
    alts = payload.get("alternatives") or []
    if isinstance(alts, str):
        alts = alts.split("\n")
    return [s for s in map(str, alts) if s.strip()]


def _part(value: Any) -> str:
    """A statement part on one line without the punctuation that ends it, or as written when
    that is all it is. A caller's trailing ellipsis stays, without what follows it."""
    text = collapse_ws(value)
    head = text.rstrip(_PART_ENDINGS)
    if not head:
        return text
    if text[len(head) :].replace(" ", "").startswith(_ELLIPSIS):
        return head + _ELLIPSIS
    return head


def states_reason(payload: dict[str, Any]) -> bool:
    """The reasoning is not the outcome itself, case, whitespace and the closing
    punctuation aside. One that is records a ban or an invariant stated with no reason."""
    reasoning = _part(payload.get("reasoning")).rstrip(_SENTENCE_ENDS + " ")
    outcome = _part(payload.get("outcome")).rstrip(_SENTENCE_ENDS + " ")
    return bool(reasoning) and reasoning.casefold() != outcome.casefold()


def memory_value(payload: dict[str, Any]) -> str:
    """The decision as one statement, the scenario as a sentence then the outcome and
    the reason, for the memory row that search embeds and memory.for_file shows.

    The category is left out: it is on the record and in the row's tag, and a slug or a
    `Scenario:` label is noise to search and to the pre-edit line. Each part is clipped so the
    statement fits the embedded excerpt; nothing parses this text.
    """
    scenario = _part(payload.get("scenario"))
    parts = []
    if scenario:
        parts.append(_sentence(_clip(scenario, STATEMENT_SCENARIO_CHARS)))
    stated = states_reason(payload)
    # With no reason stated, the outcome takes the reasoning's room: a long ban keeps its
    # "must not".
    room = STATEMENT_OUTCOME_CHARS + (0 if stated else STATEMENT_REASONING_CHARS)
    outcome = _clip(_part(payload.get("outcome")), room)
    reasoning = _part(payload.get("reasoning")) if stated else ""
    reasoning = _LEADING_BECAUSE.sub("", reasoning)
    reasoning = _clip(reasoning, STATEMENT_REASONING_CHARS)
    text = f"Decided {outcome} because {reasoning}" if reasoning else f"Decided {outcome}"
    parts.append(_sentence(text))
    rejected = [_part(a) for a in alternatives(payload)]
    if rejected:
        joined = _clip("; ".join(rejected), STATEMENT_REJECTED_CHARS)
        parts.append(_sentence(f"Rejected: {joined}"))
    realized = _clip(_part(payload.get("realized_outcome")), STATEMENT_REALIZED_CHARS)
    if realized:
        verdict = ""
        if payload.get("success") is True:
            verdict = " (succeeded)"
        elif payload.get("success") is False:
            verdict = " (failed)"
        parts.append(_sentence(f"Realized: {realized}{verdict}"))
    return " ".join(parts)
