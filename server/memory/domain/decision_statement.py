"""A decision as one statement for its memory row: what search embeds and
memory.for_file shows. Every part is kept whole: the record's own field limits bound it."""

from __future__ import annotations

import re
from typing import Any

_ELLIPSIS = "..."
# How a rejected option and its reason are stored: `<option> — rejected: <reason>`.
REJECTED = " — rejected: "


# What a part ends without: its own full stop, comma, semicolon or colon, ASCII or
# full-width, would double the statement's punctuation.
_PART_ENDINGS = " .,;:。，、；："
# A part that ends a sentence itself (a question, an exclamation, a trailing ellipsis)
# keeps its mark.
_SENTENCE_ENDS = ".!?？！…"
# A reason written as "Because …": the statement says "because" itself.
_LEADING_BECAUSE = re.compile(r"^because\b[ ,:;.…—-]*", re.IGNORECASE)


def collapse_ws(text: Any) -> str:
    """`text` on one line, each run of whitespace a single space."""
    return " ".join(str(text or "").split())


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
    punctuation aside. One that is records a decision stated with no reason."""
    reasoning = _part(payload.get("reasoning")).rstrip(_SENTENCE_ENDS + " ")
    outcome = _part(payload.get("outcome")).rstrip(_SENTENCE_ENDS + " ")
    return bool(reasoning) and reasoning.casefold() != outcome.casefold()


def memory_value(payload: dict[str, Any], *, proposed: bool = False) -> str:
    """The decision as one statement, the scenario as a sentence then the outcome and
    the reason, for the memory row that search embeds and memory.for_file shows. A
    `proposed` decision reads "Proposed <outcome>", so no reader takes it for one made.

    The category is left out: it is on the record and in the row's tag. One reader
    parses this text: the review brief's token check reads the names of the `Rejected:`
    clause up to the `Realized:` one (`checked_names` in
    `server/code/app/index/review_brief.py`).
    """
    scenario = _part(payload.get("scenario"))
    parts = []
    if scenario:
        parts.append(_sentence(scenario))
    stated = states_reason(payload)
    outcome = _part(payload.get("outcome"))
    reasoning = _part(payload.get("reasoning")) if stated else ""
    reasoning = _LEADING_BECAUSE.sub("", reasoning)
    verb = "Proposed" if proposed else "Decided"
    text = f"{verb} {outcome} because {reasoning}" if reasoning else f"{verb} {outcome}"
    parts.append(_sentence(text))
    rejected = [_part(a) for a in alternatives(payload)]
    if rejected:
        parts.append(_sentence(f"Rejected: {'; '.join(rejected)}"))
    realized = _part(payload.get("realized_outcome"))
    if realized:
        verdict = ""
        if payload.get("success") is True:
            verdict = " (succeeded)"
        elif payload.get("success") is False:
            verdict = " (failed)"
        parts.append(_sentence(f"Realized: {realized}{verdict}"))
    return " ".join(parts)
