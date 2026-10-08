"""Token budget for the memory.ask synthesis window.

`memory.ask` packs up to `memory_ask_context_memories` excerpts of at most 2,000
characters each, which is anywhere between a few hundred and several thousand input
tokens before the prompt. Every other reader in loom carries an input budget
(`context.pack` 2,000 tokens, `memory.stack` 4,000, `code.ask` 4,000); this gives the
ask the same knob, so a caller on a tight prompt, or a bench drawing an
accuracy-versus-tokens curve, fixes the INPUT rather than a count of items.

The estimate uses the measured character rate for prose-like text (Markdown 4.23 chars
per token) rather than the folk 4.0, applied to the line the synthesizer actually
renders per memory (date, key, type, excerpt), so the budget is what the model sees, not
the raw excerpt length.

Trimming keeps rank order and drops from the tail: the window is already the
ranked top of the pool, so the items past the budget are the least relevant
ones. Nothing is truncated mid-excerpt — a cut excerpt is a wrong fact.
"""

from __future__ import annotations

from typing import Any

# Measured chars-per-token rates (Markdown 4.23; Python 4.20; JSON 4.75).
CHARS_PER_TOKEN_PROSE = 4.2
# The synthesizer's excerpt cap (server.memory.app.ask.rendering._EXCERPT_CHAR_LIMIT).
_EXCERPT_CHARS = 2000
# Fixed per-line overhead the renderer adds: "[YYYY-MM-DD] [" ... "] (type) ".
_LINE_OVERHEAD_CHARS = 32


def estimate_memory_tokens(m: Any) -> int:
    """Tokens the rendered line for one memory costs the synthesizer."""

    excerpt = str(getattr(m, "content_excerpt", "") or "")[:_EXCERPT_CHARS]
    key = str(getattr(m, "memory_key", "") or "")
    chars = len(excerpt) + len(key) + _LINE_OVERHEAD_CHARS
    return max(1, int(round(chars / CHARS_PER_TOKEN_PROSE)))


def trim_to_token_budget(
    memories: list[Any], token_budget: int | None
) -> tuple[list[Any], int, int]:
    """Keep the ranked prefix of `memories` whose rendered lines fit the budget.

    Returns ``(kept, estimated_tokens_of_kept, dropped_count)``. A ``None`` or
    non-positive budget keeps everything. The first memory is always kept, so
    a budget smaller than one excerpt still answers from the top hit rather
    than from nothing (the caller's abstain logic already ran on the pool).
    """

    if not memories:
        return [], 0, 0
    if token_budget is None or int(token_budget) <= 0:
        return list(memories), sum(estimate_memory_tokens(m) for m in memories), 0
    budget = int(token_budget)
    kept: list[Any] = []
    spent = 0
    for m in memories:
        cost = estimate_memory_tokens(m)
        if kept and spent + cost > budget:
            break
        kept.append(m)
        spent += cost
    return kept, spent, len(memories) - len(kept)


__all__ = ["CHARS_PER_TOKEN_PROSE", "estimate_memory_tokens", "trim_to_token_budget"]
