"""Test-time learning is retrieval of labelled examples.

In "test-time learning", thousands of labelled examples arrive as context,
then a new input must be classified. Plain retrieval does poorly on it,
because it returns passages that *mention* the input, not examples that
*demonstrate* the label. Loom stores examples as memories with a `label:<value>` tag; this
module turns the nearest labelled memories into a vote and a few-shot
block. Pure; the handler owns the search and the tag read.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Demonstration:
    memory_key: str
    text: str
    label: str
    similarity: float


@dataclass
class Vote:
    votes: dict[str, float] = field(default_factory=dict)
    predicted: str = ""
    margin: float = 0.0  # top vote minus runner-up, both normalised to sum 1


def label_of(tags: Iterable[str], prefix: str) -> str:
    """The first tag carrying the prefix, minus the prefix; '' when none."""
    for t in tags:
        t = str(t or "")
        if t.startswith(prefix) and len(t) > len(prefix):
            return t[len(prefix) :].strip()
    return ""


def vote(demos: Sequence[Demonstration]) -> Vote:
    """Similarity-weighted vote over labels, normalised to sum 1 (kNN with
    cosine weights)."""
    raw: dict[str, float] = {}
    for d in demos:
        if not d.label:
            continue
        raw[d.label] = raw.get(d.label, 0.0) + max(0.0, float(d.similarity))
    total = sum(raw.values())
    if total <= 0:
        return Vote()
    norm = {k: round(v / total, 6) for k, v in sorted(raw.items(), key=lambda kv: (-kv[1], kv[0]))}
    ordered = list(norm.values())
    margin = ordered[0] - (ordered[1] if len(ordered) > 1 else 0.0)
    return Vote(votes=norm, predicted=next(iter(norm)), margin=round(margin, 6))


def few_shot_block(demos: Sequence[Demonstration], *, max_chars: int = 6000) -> str:
    """Demonstrations rendered for a prompt, nearest first, trimmed to a
    budget so a long example set cannot flood the caller's context."""
    lines: list[str] = []
    used = 0
    for d in demos:
        if not d.label:
            continue
        entry = f"Input: {d.text.strip()}\nLabel: {d.label}"
        if used + len(entry) > max_chars and lines:
            break
        lines.append(entry)
        used += len(entry) + 2
    return "\n\n".join(lines)
