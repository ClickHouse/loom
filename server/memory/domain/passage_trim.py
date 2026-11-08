"""Query-aware passage trimming for verbatim transcript chunks.

Reduces a multi-turn dialogue chunk to its most query-relevant turn plus one
adjacent turn on each side. Pure string ops, no LLM/embedding (hot read path).

The trim must never regress answerability, so: speaker attribution is
load-bearing (a fragment that drops who said it isn't answerable) — the unit of
selection is the TURN, markers stay glued to their text; the budget is a soft
cap — a single over-budget best turn is kept whole rather than cut below the
answer span; a chunk already under budget is returned unchanged.
"""

from __future__ import annotations

import re

# Soft per-chunk token budget (~4 chars/token); never slices the best turn
# below itself (see _trim_long_turn).
_DEFAULT_BUDGET_TOKENS = 90
_CHARS_PER_TOKEN = 4

# Splits before each speaker marker; lookahead keeps the tag with its turn.
_SPEAKER_RE = re.compile(r"(?=\[(?:user|assistant|system|tool|function)\])", re.IGNORECASE)

# Sentence boundary: after . ! ? + whitespace, keeping punctuation on the left.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Tokens shorter than this are dropped from overlap scoring (stopword-grade).
_MIN_TOKEN_LEN = 3


def _tokens(s: str) -> set[str]:
    """Lowercased alpha-only tokens, min length 3 — matches the chunk plane's
    density-scoring tokenizer so trim and rank agree on what a query term is."""
    out: set[str] = set()
    cur: list[str] = []
    for c in s.lower():
        if c.isalpha():
            cur.append(c)
        else:
            if len(cur) >= _MIN_TOKEN_LEN:
                out.add("".join(cur))
            cur = []
    if len(cur) >= _MIN_TOKEN_LEN:
        out.add("".join(cur))
    return out


def _est_tokens(s: str) -> int:
    return max(1, len(s) // _CHARS_PER_TOKEN)


def _split_turns(text: str) -> list[str]:
    """Split a chunk into speaker-prefixed turns, each keeping its marker.

    Leading text before the first marker (a chunk can start mid-turn) is kept
    as its own unit so its content isn't lost.
    """
    parts = [p for p in _SPEAKER_RE.split(text) if p.strip()]
    return parts if parts else ([text] if text.strip() else [])


def _marker_prefix(turn: str) -> str:
    """The leading "[speaker]" marker of a turn, or "" if none."""
    m = re.match(r"\s*(\[(?:user|assistant|system|tool|function)\])", turn, re.IGNORECASE)
    return m.group(1) if m else ""


def _score_unit(unit: str, q_tokens: set[str]) -> int:
    """Lexical overlap count between a unit's tokens and the query tokens."""
    if not q_tokens:
        return 0
    return len(_tokens(unit) & q_tokens)


# An enumeration is one unit. "Can you remind me what the
# other four options were?" retrieves the chunk that
# holds the assistant's numbered list of four alternatives; a sentence
# window would keep item 3 ± one sentence and the answer would name only one
# option. A list's items are what a question about the list asks for, so a
# turn with at least three enumerated lines is kept whole up to a hard cap
# instead of being windowed to its best sentence.
# A line's own leading blanks only: `^\s*` would rescan every blank line below.
_ENUM_LINE_RE = re.compile(r"(?m)^[^\S\n]*(?:\d{1,3}[.)]|[-*\u2022])\s+\S")
_ENUM_MIN_LINES = 3
_ENUM_CAP_CHARS = 1600


def is_enumeration(text: str) -> bool:
    """At least `_ENUM_MIN_LINES` lines that start with a list marker."""
    return len(_ENUM_LINE_RE.findall(text or "")) >= _ENUM_MIN_LINES


def _trim_long_turn(turn: str, q_tokens: set[str], budget_chars: int) -> str:
    """Sentence-window the best sentence ±1 inside one over-budget turn.

    Always re-prepends the speaker marker. If the best sentence alone exceeds
    budget it's returned whole — soft cap, answerability wins. An enumeration
    is returned whole up to `_ENUM_CAP_CHARS`.
    """
    if is_enumeration(turn) and len(turn) <= _ENUM_CAP_CHARS:
        return turn.strip()
    marker = _marker_prefix(turn)
    body = (turn[len(marker) :] if marker else turn).lstrip()
    sentences = [s for s in _SENTENCE_RE.split(body) if s.strip()]
    if len(sentences) <= 1:
        return turn.strip()
    scores = [_score_unit(s, q_tokens) for s in sentences]
    best = max(range(len(sentences)), key=lambda i: (scores[i], -i))
    lo, hi = max(0, best - 1), min(len(sentences), best + 2)
    window = " ".join(s.strip() for s in sentences[lo:hi])
    out = f"{marker} {window}".strip() if marker else window
    if _est_tokens(out) <= budget_chars // _CHARS_PER_TOKEN:
        return out
    return f"{marker} {sentences[best].strip()}".strip() if marker else sentences[best].strip()


def trim_chunk_excerpt(
    excerpt: str,
    query_text: str,
    *,
    budget_tokens: int = _DEFAULT_BUDGET_TOKENS,
) -> str:
    """Reduce a transcript chunk excerpt to its most query-relevant passage.

    Returns the best-scoring turn plus one adjacent turn on each side, soft-
    capped at `budget_tokens`. Returned unchanged when already under budget or
    there's no query signal to score against (trimming blind risks the answer).
    """
    if not excerpt:
        return excerpt
    budget_chars = budget_tokens * _CHARS_PER_TOKEN
    if len(excerpt) <= budget_chars:
        return excerpt
    q_tokens = _tokens(query_text or "")
    if not q_tokens:
        return excerpt

    turns = _split_turns(excerpt)
    if len(turns) <= 1:
        return _trim_long_turn(turns[0] if turns else excerpt, q_tokens, budget_chars)

    scores = [_score_unit(t, q_tokens) for t in turns]
    if max(scores) == 0:
        # No lexical overlap anywhere — any window is arbitrary, keep the whole.
        return excerpt

    best = max(range(len(turns)), key=lambda i: (scores[i], -i))
    lo, hi = max(0, best - 1), min(len(turns), best + 2)
    window_idx = list(range(lo, hi))
    window = "\n".join(turns[i].strip() for i in window_idx)

    if _est_tokens(window) <= budget_tokens:
        return window

    # Over budget: never drop a turn from the best±1 window — the answer entity
    # is often a novel token in the adjacent turn that the higher-scoring
    # question turn lacks. Only sentence-window turns that individually blow
    # the budget; soft cap, answerability wins.
    pieces: list[str] = []
    for i in window_idx:
        turn = turns[i]
        if _est_tokens(turn) > budget_tokens:
            turn = _trim_long_turn(turn, q_tokens, budget_chars)
        pieces.append(turn.strip())
    return "\n".join(pieces)


# --- chunk<->fact / chunk<->chunk near-duplicate detection ------------------


def _content_token_set(s: str) -> set[str]:
    """Token set for containment / Jaccard — set grain ignores word order."""
    return _tokens(s)


def token_set_is_near_duplicate(
    c: set[str],
    k: set[str],
    *,
    containment_threshold: float = 0.9,
    jaccard_threshold: float = 0.8,
) -> bool:
    """`is_near_duplicate` over already-tokenized passages — lets an O(n²)
    dedup pool tokenize each passage once and reuse the set per pairwise call.
    """
    if len(c) < 4 or not k:
        return False
    inter = len(c & k)
    containment = inter / len(c)
    if containment >= containment_threshold:
        return True
    union = len(c | k)
    jaccard = inter / union if union else 0.0
    return jaccard >= jaccard_threshold


def is_near_duplicate(
    candidate: str,
    kept: str,
    *,
    containment_threshold: float = 0.9,
    jaccard_threshold: float = 0.8,
) -> bool:
    """True when `candidate` is substantially the same text as a kept item.

    Conservative — collapses only on containment (candidate's tokens almost a
    subset of kept's) or near-identical Jaccard, so a chunk is never dropped
    when it carries an entity the kept item lacks. Tiny passages (< 4 tokens)
    never dedup: too little signal to distinguish same-evidence from coincidence.
    """
    return token_set_is_near_duplicate(
        _content_token_set(candidate),
        _content_token_set(kept),
        containment_threshold=containment_threshold,
        jaccard_threshold=jaccard_threshold,
    )
