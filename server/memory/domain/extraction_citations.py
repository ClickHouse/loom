"""A fact cites the turn it came from.

The extractor names its sources as message indices, and about 2% of rows
cite a turn that shares at most half the content words the best user turn
in the same session shares with the value — "replaced spark plugs … on
2023-03-15" cited the brake-pad question, not the sentence that said "a
while back … after replacing the spark plugs". Everything that reads the
citation downstream — the dropped-phrase recoveries, evidence links,
erasure — then works from the wrong turn.

The repair is content overlap, the same measure the dropped-phrase recovery
uses to align a value with a sentence: when the best user turn shares at least
`min_shared` content words with the value and every cited turn shares at
most half that, the best turn becomes the first citation; cited turns
that still share at least two words stay behind it. A fact whose cited
turn IS the best match, or whose value is too short to score, is never
touched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

_TOKEN_RE = re.compile(r"[a-z]{3,}")
_STOP = frozenset(
    "the and for with that this from his her their its was were been have has had got get about "
    "just really very some they them she him you your our who what when which into onto user "
    "assistant also been being would could should might there here then than new during after "
    "before while like over under about because since still already only even much more most "
    "considering thinking planning looking want wants wanted need needs".split()
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP}


def _is_user(m: Mapping[str, Any]) -> bool:
    return str(m.get("role") or "").lower() in ("", "user", "human")


def repair_citations(
    facts: Sequence[Any],
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    min_shared: int = 4,
    min_value_tokens: int = 4,
) -> list[Any]:
    """Facts with `source_message_indices` re-pointed at the user turn they
    match best, when the cited turns clearly do not match. Unchanged facts
    are returned as-is (same objects)."""
    if not facts or not messages:
        return list(facts)
    user_tokens = [_tokens(str(m.get("content") or "")) if _is_user(m) else set() for m in messages]
    out: list[Any] = []
    for f in facts:
        vt = _tokens(str(getattr(f, "value", "") or ""))
        if len(vt) < min_value_tokens:
            out.append(f)
            continue
        scores = [len(vt & ut) for ut in user_tokens]
        best_idx = max(range(len(scores)), key=lambda i: scores[i])
        best = scores[best_idx]
        raw_cited = getattr(f, "source_message_indices", None) or []
        cited = [i for i in raw_cited if 0 <= i < len(scores)]
        cited_best = max((scores[i] for i in cited), default=0)
        if best < min_shared or best_idx in cited or cited_best * 2 > best:
            out.append(f)
            continue
        keep = [i for i in cited if scores[i] >= 3]
        out.append(replace(f, source_message_indices=[best_idx, *keep][:3]))
    return out
