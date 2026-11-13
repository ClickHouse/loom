"""Knowledge gaps: how a missed query is normalised, and how the daily worker merges
the groups of near-identical queries and keeps only those enough people asked. Pure.

A group is the misses of one normalised query that enough distinct people asked in that
form (the store drops the rest, so one person's question never reaches an admin however
often they ask it). Two groups whose queries embed close enough are one gap: "what is
arr" and "what s our arr".
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from server.core.hashing import canonical_document_text

# The other queries merged into a gap that it shows, at most.
MAX_SAMPLES = 5  # not a setting: the size of the tile's sample list
# Kept inside a word beside letters and numbers, so `C++`, `C#` and `arr_v2` stay whole.
_WORD_SYMBOLS = frozenset("_+#")


def normalize_query(query: str) -> str | None:
    """`query` with format characters dropped, `canonical_document_text` applied and
    case folded, then every run of characters other than letters, numbers, `_`, `+` and
    `#` made one space and the ends trimmed, so two spellings of one question group
    together. A combining mark stays with the letter or number before it; a mark with
    none before it counts as punctuation. None for text that is not valid Unicode (a
    lone surrogate), which records no miss."""
    try:
        query.encode("utf-8")
    except UnicodeEncodeError:
        return None
    text = "".join(c for c in query if unicodedata.category(c) != "Cf")
    text = canonical_document_text(text).casefold()
    out: list[str] = []
    after_base = False  # the last kept character is a letter or number, or a mark on one
    for char in text:
        kind = unicodedata.category(char)[0]
        if kind in "LN":
            out.append(char)
            after_base = True
        elif kind == "M" and after_base:
            out.append(char)
        elif char in _WORD_SYMBOLS:
            out.append(char)
            after_base = False
        else:
            if out and out[-1] != " ":
                out.append(" ")
            after_base = False
    return "".join(out).strip()


def memory_missed(abstain: bool, retrieval_risk: str) -> bool:
    """Whether a memory search found nothing usable: it abstained or answered at high
    retrieval risk. loom_search's memory section also misses when the precision floor
    withheld every hit."""
    return abstain or retrieval_risk in ("high", "abstain")


def unsettled(*, degraded: object, pending_extraction: int, stale_dropped: int) -> bool:
    """Whether a search's empty answer says nothing yet: a degraded leg, writes still
    waiting for extraction, or hits the memory authority dropped."""
    return bool(degraded or pending_extraction or stale_dropped)


def gap_id(org: str, namespace: str, head: str) -> str:
    """A gap's stable id: the same head in the same namespace is the same gap on every
    build. The org is part of it, so two orgs' namespaces of one name never share an id."""
    return hashlib.sha256(f"{org}\x00{namespace}\x00{head}".encode()).hexdigest()[:32]


@dataclass
class Group:
    """The misses of one normalised query."""

    head: str
    asks: int
    last_at: datetime
    # Distinct askers, each the person a token stood for (folded to one spelling).
    askers: set[str]
    # The normalised queries merged into this one.
    samples: list[str] = field(default_factory=list)

    def absorb(self, other: Group) -> None:
        self.asks += other.asks
        self.last_at = max(self.last_at, other.last_at)
        self.samples = _first_unique([*self.samples, other.head, *other.samples], MAX_SAMPLES)
        self.askers |= other.askers


def merge_groups(
    groups: Sequence[Group], vectors: Sequence[Sequence[float]], min_similarity: float
) -> list[Group]:
    """`groups` (most asked first) with each folded into the first earlier group whose
    head's vector is at least `min_similarity` alike, by cosine. Greedy, so a group
    joins the most asked gap it is close to. CPU-bound: the worker runs it in a thread."""
    kept: list[tuple[Group, list[float]]] = []
    for group, vec in zip(groups, vectors, strict=True):
        unit = _unit(vec)
        for gap, gap_unit in kept:
            if math.sumprod(unit, gap_unit) >= min_similarity:
                gap.absorb(group)
                break
        else:
            kept.append((group, unit))
    return [g for g, _ in kept]


def ranked(gaps: Sequence[Group]) -> list[Group]:
    """Most distinct askers first, then most asks."""
    return sorted(gaps, key=lambda g: (len(g.askers), g.asks), reverse=True)


def _unit(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else [0.0] * len(vec)


def _first_unique(items: Sequence[str], limit: int) -> list[str]:
    out: dict[str, None] = {}
    for item in items:
        if item:
            out.setdefault(item, None)
    return list(out)[:limit]
