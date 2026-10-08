"""Block-level token accounting for the synthesis prompt.

Half of a strong-tier `memory.ask` prompt is not the evidence window (input p50
6,483 tokens, window 3,052). The rest is the system prompt, the per-call notes,
the knowledge passages, the code-grounding and identifier-chase rows, the raw
turns of the deep pass, the timeline and the question; each block has its own
paired gain, and this accounting shows them next to one another.

This module is the pure part: given the texts the renderer composed, the
estimated tokens per block. The estimate is the listing budget's `chars / 4`
— the provider reports one input-token total per
call, never per block, so the blocks are estimates and the total is what the
ledger's `synth_input_tokens` checks them against. `other` is what no block
claims (headers, separators, session labels, the attribution line): the
accounting is complete by construction, and `other` is the unattributed
share the gate reads.

Accumulated per ask over its synthesis-family calls in a context variable
(the shape of `_SYNTH_CALLS`), so a two-call ask reports the tokens it paid,
not the last prompt's. Never raises; a renderer that cannot account a prompt
reports nothing rather than failing the answer.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable
from typing import Any

# Canonical order: the static head, what the call adds, the evidence by kind,
# the per-ask context blocks in the order the prompt lays them out, the tail.
BLOCKS: tuple[str, ...] = (
    "system",
    "notes",
    "window",
    "chunks",
    "code",
    "chase",
    "knowledge",
    "raw_turns",
    "timeline",
    "recent",
    "scratchpad",
    "elision",
    "graph",
    "question",
    "other",
)

CHARS_PER_TOKEN = 4

_ACCUMULATED: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "loom_prompt_blocks", default=None
)


def tokens_of(chars: int) -> int:
    """The listing budget's estimate: whole tokens at four characters each."""
    return max(0, int(chars)) // CHARS_PER_TOKEN


def row_block(m: Any) -> str:
    """Which evidence block one window row belongs to: `code` for a code-plane
    row, `chase` for a row the identifier chase added, `chunks` for a verbatim
    chunk row, `window` for a fact or session-summary row."""
    if str(getattr(m, "source", "") or "") == "code_index":
        return "code"
    if str(getattr(m, "explanation", "") or "").startswith("identifier chase"):
        return "chase"
    key = str(getattr(m, "memory_key", "") or "")
    if str(getattr(m, "source", "") or "") == "chunk" or key.startswith("chunk:"):
        return "chunks"
    return "window"


def row_chars(rows: Iterable[tuple[Any, str]]) -> dict[str, int]:
    """Characters per evidence block for `(row, rendered line)` pairs."""
    out: dict[str, int] = {}
    for m, line in rows:
        b = row_block(m)
        out[b] = out.get(b, 0) + len(line)
    return out


def block_tokens(
    *,
    total_chars: int,
    parts: dict[str, int],
) -> dict[str, int]:
    """Tokens per block from character counts. `parts` holds the characters
    each named block claims; whatever is left of `total_chars` is `other`
    (never negative — a layout that renders a row twice, or a near-duplicate
    group that renders variants as diffs, cannot push it below zero). Only
    the blocks present are returned, in canonical order."""
    claimed = 0
    out: dict[str, int] = {}
    for name in BLOCKS:
        if name == "other":
            continue
        chars = int(parts.get(name, 0) or 0)
        if chars > 0:
            out[name] = tokens_of(chars)
            claimed += chars
    other = tokens_of(max(0, int(total_chars) - claimed))
    if other > 0:
        out["other"] = other
    return out


def reset() -> None:
    """A new ask: nothing accounted yet."""
    _ACCUMULATED.set({})


def add(blocks: dict[str, int]) -> None:
    """Fold one synthesis call's blocks into the ask's total. No ask bound
    (a lone call in a test, a worker): the call's blocks become the total."""
    acc = dict(_ACCUMULATED.get() or {})
    for k, v in blocks.items():
        acc[k] = int(acc.get(k, 0)) + int(v)
    _ACCUMULATED.set(acc)


def current() -> dict[str, int]:
    """The ask's blocks so far, in canonical order, zeros dropped."""
    acc = _ACCUMULATED.get() or {}
    return {k: int(acc[k]) for k in BLOCKS if int(acc.get(k, 0) or 0) > 0}


__all__ = [
    "BLOCKS",
    "CHARS_PER_TOKEN",
    "add",
    "block_tokens",
    "current",
    "reset",
    "row_block",
    "row_chars",
    "tokens_of",
]
