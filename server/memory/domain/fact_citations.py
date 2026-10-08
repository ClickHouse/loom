"""Rows that cite a fact defer to it.

A chunk, digest or observation that names a fact key in the pool — or that
is itself the record of a Loom recall — is derivative of that fact: the fact
is what the user wrote and edits, the row is what a session said about it.
Search fusion and the context-block assembler both cap such rows just under
the facts they cite; the answer-first transport reads the same recall
signature.
"""

from __future__ import annotations

import re
from typing import Any

# How a memory key appears in text a model wrote or a digest restated — the reader block
# labels every memory `[key]`, the stored excerpt carries `(#key)`, and an answer quotes
# it in backticks.
_KEY_CITE_RE = re.compile(
    r"(?:`([A-Za-z][A-Za-z0-9]*(?:[_:][A-Za-z0-9]+)+)`"
    r"|#([A-Za-z][A-Za-z0-9]*(?:[_:][A-Za-z0-9]+)+)"
    r"|\[([A-Za-z][A-Za-z0-9]*(?:[_:][A-Za-z0-9]+)+)\])"
)


def cited_keys(text: str) -> list[str]:
    """Memory keys `text` cites, in order, deduped."""
    out: list[str] = []
    for m in _KEY_CITE_RE.finditer(str(text or "")):
        k = m.group(1) or m.group(2) or m.group(3) or ""
        if k and k not in out:
            out.append(k)
    return out


# The words a transcript uses when it recalls from Loom. A chunk carrying them is the
# record of a lookup — "I'll check Loom for your saved home location. You live in
# Ujjain, saved in Loom as `where_i_live`" — not a statement about the user; it is
# derivative of the facts it recalled, whether or not it names their keys.
_RECALL_SIGNATURE_RE = re.compile(
    r"(?i)(?:saved in loom|check(?:ing|ed)? loom|loom_search\b|loom for your saved|"
    r"from loom(?:'s)? memory|in loom (?:under|as) (?:the key )?`|recorded in loom|"
    r"loom_memory_(?:get|set)\b)"
)


def recall_echo(hit: Any) -> bool:
    """A non-fact row whose text is a Loom recall (see _RECALL_SIGNATURE_RE)."""
    if (getattr(hit, "source", "") or "fact") == "fact":
        return False
    return bool(_RECALL_SIGNATURE_RE.search(str(getattr(hit, "content_excerpt", "") or "")))


def defer_citing_rows(hits: list[Any], *, epsilon: float = 1e-3) -> int:
    """A non-fact row (chunk, summary, observation, …) whose excerpt cites a
    fact key that is in the pool is derivative of that fact: cap its rank_score just
    under the weakest fact it cites. The fact is what the user wrote and edits; the
    digest is what a session said about it. A row that is a Loom recall (`recall_echo`)
    is derivative of the facts it recalled even when it names none of their keys — it is
    capped under the weakest fact in the pool. Returns how many rows were capped."""
    facts: dict[str, Any] = {}
    for h in hits:
        if (getattr(h, "source", "") or "fact") == "fact":
            key = str(getattr(h, "memory_key", "") or "")
            if key and key not in facts:
                facts[key] = h
    if not facts:
        return 0
    capped = 0
    weakest_fact = min(float(f.rank_score) for f in facts.values())
    for h in hits:
        if (getattr(h, "source", "") or "fact") == "fact":
            continue
        cited = [k for k in cited_keys(getattr(h, "content_excerpt", "")) if k in facts]
        echo = recall_echo(h)
        if not cited and not echo:
            continue
        cap = (min(float(facts[k].rank_score) for k in cited) if cited else weakest_fact) - epsilon
        if float(h.rank_score) <= cap:
            continue
        comps = getattr(h, "score_components", None)
        if comps is None:
            comps = {}
            try:
                h.score_components = comps
            except Exception:  # noqa: BLE001 — a frozen hit keeps its score
                continue
        # "*": a recall echo that names no key
        comps["cites_fact"] = cited[0] if cited else "*"
        if echo:
            comps["recall_echo"] = 1.0
        comps["rank_score_before_defer"] = float(h.rank_score)
        comps["rank_score"] = cap
        h.rank_score = cap
        capped += 1
    return capped
