"""A summary-shaped question goes to the whole document.

`memory.ask` retrieves memories and synthesizes over them. A question like
"summarize the incident report" or "what is this document about overall"
has no answer in any fifteen memories: this long-range understanding needs a
full read, and retrieval loses to one. `knowledge.synthesize` is that full read.
This module decides, without a model call, whether a question asks for the
whole of something, and which documents "something" is, from where the
knowledge search concentrates.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

_CORPUS_TASK = re.compile(
    r"\b(summar(y|ize|ise|ies)|overview|outline|synopsis|recap|tl;?dr|gist|"
    r"main (points|themes|ideas|arguments)|key (points|takeaways)|"
    r"what (is|are|was) (this|the|these) (document|book|paper|report|file|chapter|"
    r"transcript|thread|spec|rfc|article)s? (about|saying)|"
    r"(across|throughout|over) the (whole|entire|full) (document|book|paper|report|"
    r"text|transcript|thread|corpus))\b",
    re.IGNORECASE,
)


def is_corpus_task(query: str) -> bool:
    """True when the question asks for the whole of a text rather than a
    fact in it. Deterministic on purpose: the route must be predictable."""
    return bool(_CORPUS_TASK.search(query or ""))


def pick_documents(
    hits: Iterable[Any],
    *,
    max_docs: int = 3,
    min_hits: int = 2,
    min_share: float = 0.5,
) -> list[str]:
    """Documents the knowledge search concentrates on: source_uris ordered by
    hit count, keeping those with at least `min_hits` hits, until the kept
    documents hold `min_share` of all hits or `max_docs` is reached. Empty
    when the hits are spread thin (then a whole-document read has no target)."""
    counts: Counter[str] = Counter()
    order: dict[str, int] = {}
    for i, h in enumerate(hits):
        uri = str(getattr(h, "source_uri", "") or "")
        if not uri:
            continue
        counts[uri] += 1
        order.setdefault(uri, i)
    if not counts:
        return []
    total = sum(counts.values())
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], order[kv[0]]))
    chosen: list[str] = []
    covered = 0
    for uri, n in ranked:
        if n < min_hits and chosen:
            break
        if n < min_hits and not chosen:
            return []
        chosen.append(uri)
        covered += n
        if len(chosen) >= max_docs or covered / total >= min_share:
            break
    return chosen


def route_reason(docs: Sequence[str]) -> str:
    return f"knowledge_synthesize:{len(docs)}_docs"
