"""Grounding: how a fact is tied to the turns it came from, graded by the
server after extraction, never by the model's own say-so.

Loom's extractor is evidence-bound by prompt — every fact cites
`source_message_indices` — but nothing verified that the citation holds,
and `confidence` is a number the writer chose about itself. This module
anchors each extracted value to a turn by locating it as a substring, keeps
the anchor kind ordinal (verbatim > observed > derived) and represents a
failed anchor explicitly as unresolved rather than as a fabricated score.

  verbatim    the fact's `evidence` quote occurs in a cited turn (or, failing
              that, in any current turn) after whitespace/case/quote
              normalisation
  unresolved  the model gave a quote and it occurs in no turn — the quote is
              a paraphrase; the fact is kept, the quote is kept, and the
              state says so
  observed    no quote, but the cited turn indices exist — the fact is tied
              to specific turns without a verbatim span
  derived     neither — nothing ties the fact to a turn

Pure functions; the extractor calls `grade_facts` before the judge so the
judge can weigh the grade too.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Any

VERBATIM = "verbatim"
OBSERVED = "observed"
DERIVED = "derived"
UNRESOLVED = "unresolved"

_MIN_QUOTE_CHARS = 3
_MAX_QUOTE_CHARS = 400
_WS = re.compile(r"\s+")
_QUOTES = re.compile(r"[\"'“”‘’`]")
_EDGE_PUNCT = re.compile(r"^[\s\"'“”‘’`.,;:!?()\[\]-]+|[\s\"'“”‘’`.,;:!?()\[\]-]+$")


def normalize(text: str) -> str:
    """NFKC, casefold, collapse whitespace, strip edge quotes/punctuation —
    the same on both sides, so a quote and its source compare as text."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    # quotes vanish entirely — a model quoting `"WaterMinder"` and a turn
    # saying “WaterMinder” must compare as the same span
    t = _QUOTES.sub("", t)
    t = _WS.sub(" ", t).casefold().strip()
    return _EDGE_PUNCT.sub("", t)


def _turn_texts(messages: list[dict[str, Any]]) -> list[str]:
    return [normalize(str(m.get("content", "") or "")) for m in messages]


def grade(
    evidence: str,
    source_message_indices: list[int],
    turns: list[str],
) -> tuple[str, str]:
    """Return (grade, evidence_kept). `turns` are already-normalised texts."""
    quote = normalize(evidence)[:_MAX_QUOTE_CHARS]
    valid_idx = [i for i in source_message_indices if isinstance(i, int) and 0 <= i < len(turns)]
    if len(quote) >= _MIN_QUOTE_CHARS:
        cited = [turns[i] for i in valid_idx]
        if any(quote in t for t in cited) or any(quote in t for t in turns):
            return VERBATIM, quote
        return UNRESOLVED, quote
    if valid_idx:
        return OBSERVED, ""
    return DERIVED, ""


def grade_facts(facts: list[Any], messages: list[dict[str, Any]]) -> list[Any]:
    """Grade every fact (dataclass with `evidence`, `source_message_indices`,
    `grounding`) against the current turns; returns graded copies."""
    turns = _turn_texts(messages)
    out = []
    for f in facts:
        g, kept = grade(
            str(getattr(f, "evidence", "") or ""),
            list(getattr(f, "source_message_indices", []) or []),
            turns,
        )
        out.append(replace(f, grounding=g, evidence=kept))
    return out
