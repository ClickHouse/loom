"""Decision-preserving merge guard.

Two memories may share one state only when a single answer is near-optimal for
both. Descriptive similarity is the wrong test for that (Spearman 0.103, AUC
0.548 against evidence compatibility on conversation QA), and loom's consolidation merges
clusters by cosine alone. This module is the cheap, model-free half of that
criterion: a merged text
that drops a date, a number, an identifier, a proper name or a negation that
one of its sources carried cannot answer every question the sources
answered, so the merge is refused. It cannot certify that a merge is safe;
it certifies the cases that are not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|"
    "june|july|august|september|october|november|december"
)
_DATE_RES = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\.?,?\s+\d{{4}}\b", re.I),
    re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.I),
    re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{4}}\b", re.I),
    re.compile(r"\b(?:19|20)\d{2}\b"),
)
# Quantities: a number with a unit or currency, a percentage, or any number
# of two or more digits; single digits alone ("one of 3") are too common to
# be distinguishing on their own.
_QUANTITY_RE = re.compile(
    r"(?<!\w)(?:[$€£]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:%|percent|"
    r"[kmgt]b|kb|mb|gb|tb|ms|s|sec|min|h|hrs?|hours?|days?|weeks?|months?|years?|"
    r"km|mi|miles?|kg|lbs?|usd|eur)|\d{2,}[\d,]*(?:\.\d+)?)(?!\w)",
    re.I,
)
# Identifiers: issue/PR refs, versions, paths, symbols with dots or
# underscores, hostnames.
_IDENT_RE = re.compile(
    r"(?:#[\w-]+|\bv\d+(?:\.\d+)+\b|\b[\w-]+(?:/[\w.-]+)+\b|\b[a-z_]+(?:\.[a-z_]+)+\b|"
    r"\b[A-Za-z]+_[A-Za-z0-9_]+\b)"
)
# Proper names: a capitalised word not at sentence start, or two or more
# capitalised words in a row anywhere.
_NAME_RUN_RE = re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_CAP_WORD_RE = re.compile(r"\b([A-Z][a-z]{2,})\b")
_COMMON_STARTERS = frozenset(
    "the a an on in at by for from with and but or so if when while after before "
    "she he they it we you i this that these those her his their its our your my "
    "there here then now yes no not later earlier last first next also however".split()
)
_NEGATION_RE = re.compile(
    r"\b(?:not|never|no longer|isn't|aren't|wasn't|weren't|doesn't|don't|didn't|"
    r"won't|can't|cannot|couldn't|without|stopped|quit|no)\b",
    re.I,
)


def _norm(v: str) -> str:
    return re.sub(r"[\s,]+", " ", v.strip().lower()).rstrip(".")


def distinguishing_values(text: str) -> dict[str, set[str]]:
    """The values that make a memory say something another memory does not:
    dates, quantities, identifiers, proper names, and whether it negates.
    Normalised for comparison; pure."""
    t = text or ""
    dates = {_norm(m.group(0)) for r in _DATE_RES for m in r.finditer(t)}
    # a year or month-year that is part of a fuller date is not a second value
    dates = {d for d in dates if not any(d != f and d in f for f in dates)}
    quantities = {_norm(m.group(0)) for m in _QUANTITY_RE.finditer(t)}
    quantities = {q for q in quantities if not any(q in d for d in dates)}
    idents = {_norm(m.group(0)) for m in _IDENT_RE.finditer(t)}
    names = {_norm(m.group(0)) for m in _NAME_RUN_RE.finditer(t)}
    names |= {
        _norm(m.group(1))
        for m in _CAP_WORD_RE.finditer(t)
        if m.group(1).lower() not in _COMMON_STARTERS
        and not re.fullmatch(_MONTHS, m.group(1).lower())
    }
    negation = {"negated"} if _NEGATION_RE.search(t) else set()
    return {
        "dates": dates,
        "quantities": quantities,
        "identifiers": idents,
        "names": names,
        "negation": negation,
    }


@dataclass
class MergeVerdict:
    ok: bool
    # per source index: the values the merged text lost, by kind
    lost: dict[int, dict[str, list[str]]] = field(default_factory=dict)

    def counts(self) -> str:
        """The kinds of value each source lost, as counts: the values never leave it."""
        return "; ".join(
            f"source[{i}] {kind}: {len(vals)}"
            for i, kinds in sorted(self.lost.items())
            for kind, vals in sorted(kinds.items())
        )


def merge_preserves(sources: list[str], merged: str) -> MergeVerdict:
    """True when every distinguishing value of every source survives in the
    merged text (dates, quantities, identifiers, names by normalised
    substring; negation by presence). The check is one-sided: a passing
    merge may still be wrong, a failing one has certainly lost a value."""
    merged_vals = distinguishing_values(merged)
    merged_norm = _norm(merged)
    lost: dict[int, dict[str, list[str]]] = {}
    for i, src in enumerate(sources):
        vals = distinguishing_values(src)
        for kind in ("dates", "quantities", "identifiers", "names"):
            missing = [v for v in vals[kind] if v not in merged_vals[kind] and v not in merged_norm]
            if missing:
                lost.setdefault(i, {})[kind] = missing
        if vals["negation"] and not merged_vals["negation"]:
            lost.setdefault(i, {})["negation"] = ["negation dropped"]
    return MergeVerdict(ok=not lost, lost=lost)


__all__ = ["MergeVerdict", "distinguishing_values", "merge_preserves"]
