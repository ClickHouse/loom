"""LLM-free relevance cut over the memory.ask synthesis window.

Retrieval ranks; it never says no. `similarity_floor_cut` drops the off-topic
tail of the window by similarity relative to the best row, never below a floor.
The bench's LLM judge (`bench/relevance_gate.py`) reuses `GateVerdict`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GateVerdict:
    kept: list[Any]
    dropped: list[Any] = field(default_factory=list)
    # "knee"     — similarity_floor_cut dropped rows (no LLM call)
    # "skipped"  — window at/below the floor, no call made
    # "kept_all" — the judge kept every row (or we failed open)
    # "filtered" — rows dropped, kept >= floor
    # "floor"    — the judge kept fewer than the floor; top-ranked rows restored
    # "error"    — call/parse failed, full window kept
    mode: str = "skipped"
    judged: int = 0

    @property
    def dropped_keys(self) -> list[str]:
        return [str(getattr(m, "memory_key", "") or "") for m in self.dropped]


def similarity_floor_cut(memories: list[Any], *, rel_floor: float, min_keep: int) -> GateVerdict:
    """LLM-free window cut: keep the rows whose cosine
    similarity is at least `rel_floor` × the best row's, never fewer than
    `min_keep`. Membership, not order — the retriever's (or reranker's)
    ordering of the kept rows is untouched.

    An adaptive-k cut, applied relative to the top score rather than to a fixed threshold,
    so it adapts per query: a question whose best memory scores 0.34 keeps everything above
    0.25, one whose best scores 0.60 keeps everything above 0.45. The on-topic rows for a
    chat-history question cluster within ~0.05 of the top while the off-topic tail sits
    0.1–0.25 below it — a gap an LLM judge over-cuts and this cut finds without a call.
    rel_floor <= 0 = off."""
    n = len(memories)
    if rel_floor <= 0 or n <= max(1, min_keep):
        return GateVerdict(kept=list(memories), mode="skipped", judged=0)

    def _sim(m: Any) -> float:
        s = getattr(m, "similarity", None)
        if s is None and isinstance(m, dict):
            s = m.get("similarity")
        if s is not None:
            return float(s)
        d = getattr(m, "distance", None)
        if d is None and isinstance(m, dict):
            d = m.get("distance")
        return 1.0 - float(d) if d is not None else 0.0

    sims = [_sim(m) for m in memories]
    top = max(sims)
    if top <= 0:
        return GateVerdict(kept=list(memories), mode="kept_all", judged=n)
    threshold = top * float(rel_floor)
    keep_idx = {i for i, s in enumerate(sims) if s >= threshold}
    if len(keep_idx) >= n:
        return GateVerdict(kept=list(memories), mode="kept_all", judged=n)
    mode = "knee"
    if len(keep_idx) < min_keep:
        # Floor by the retriever's rank, exactly as the judge path does.
        for i in range(n):
            if len(keep_idx) >= min_keep:
                break
            keep_idx.add(i)
        mode = "floor"
        if len(keep_idx) >= n:
            return GateVerdict(kept=list(memories), mode="kept_all", judged=n)
    kept = [m for i, m in enumerate(memories) if i in keep_idx]
    dropped = [m for i, m in enumerate(memories) if i not in keep_idx]
    return GateVerdict(kept=kept, dropped=dropped, mode=mode, judged=n)
