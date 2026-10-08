"""A needle question gets the sentence, not the chunk.

Smaller chunks help accurate retrieval and hurt long-range understanding
(an agent-memory bench shows both): one chunk size cannot serve both. Loom chunks by
section (1,500 characters) for the long-range case. This module gives
needle-shaped reads the small-chunk benefit at read time, without re-ingesting:
the retrieved chunk is split into short sentence windows, the windows are
embedded in one batch, and the window nearest the query is returned as the hit's `span` beside the
full chunk. Pure: the embedder is injected.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
# "Dr. Smith", "J. Doe": a one-to-three-letter capitalised token before the
# period is a title or initial, not a sentence end.
_ABBREV = re.compile(r"\b[A-Z][a-z]{0,2}\.$")


@dataclass(frozen=True)
class Span:
    text: str
    start: int  # character offset in the chunk
    end: int
    similarity: float


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) character offsets of sentences in `text`, in order.
    Conservative split: only after terminal punctuation followed by a
    capital, digit or opening quote, so abbreviations and decimals survive."""
    out: list[tuple[int, int]] = []
    pos = 0
    for m in _SENT.finditer(text):
        end = m.start()
        if _ABBREV.search(text[:end]):
            continue
        if end > pos:
            out.append((pos, end))
        pos = m.end()
    if pos < len(text) and text[pos:].strip():
        out.append((pos, len(text)))
    return out


def windows(text: str, *, size: int = 2, stride: int = 1) -> list[tuple[int, int]]:
    """Sliding windows of `size` sentences with `stride`, as offsets; the
    whole text when it has fewer than `size` sentences."""
    sents = sentences(text)
    if len(sents) <= size:
        return [(0, len(text))] if text.strip() else []
    last = len(sents) - size + 1
    return [(sents[i][0], sents[min(i + size, len(sents)) - 1][1]) for i in range(0, last, stride)]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=False))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


EmbedMany = Callable[[list[str]], Awaitable[list[list[float]]]]


async def best_spans(
    query_vec: Sequence[float],
    chunks: Sequence[str],
    embed_many: EmbedMany,
    *,
    size: int = 2,
    max_windows_per_chunk: int = 24,
) -> list[Span | None]:
    """For each chunk, the sentence window nearest the query, or None when
    the chunk is a single window (the chunk already is the span). All
    windows of all chunks are embedded in ONE call."""
    plan: list[list[tuple[int, int]]] = []
    texts: list[str] = []
    for c in chunks:
        ws = windows(c, size=size)[:max_windows_per_chunk]
        plan.append(ws if len(ws) > 1 else [])
        texts.extend(c[s:e] for s, e in (ws if len(ws) > 1 else []))
    if not texts:
        return [None for _ in chunks]
    vectors = await embed_many(texts)
    out: list[Span | None] = []
    k = 0
    for c, ws in zip(chunks, plan, strict=False):
        if not ws:
            out.append(None)
            continue
        best: Span | None = None
        for s, e in ws:
            sim = cosine(query_vec, vectors[k]) if k < len(vectors) else 0.0
            k += 1
            if best is None or sim > best.similarity:
                best = Span(text=c[s:e].strip(), start=s, end=e, similarity=round(sim, 6))
        out.append(best)
    return out
