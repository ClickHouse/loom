"""memory.brief: an orientation read over memory.ask — the answer, the evidence
it cites, what is unknown and what to read next (server/memory/api/models_brief.py
has the wire models)."""

from __future__ import annotations

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_brief import BriefEvidence, MemoryBriefInput, MemoryBriefOutput
from server.memory.api.models_common import _Identity

_EXCERPT_CAP = 240


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for it in items:
        s = (it or "").strip()
        if s:
            seen.setdefault(s, None)
    return list(seen)


async def handle_memory_brief(input: MemoryBriefInput) -> MemoryBriefOutput:
    from server.memory.app.ask.handler import handle_memory_ask

    identity = {k: getattr(input, k) for k in _Identity.model_fields}
    ask = MemoryAskInput(
        **identity,
        query=input.query,
        top_k=input.top_k,
        tags=list(input.tags),
        allow_synthesis=True,
        reinforce=False,  # orientation read, not reliance
    )
    out = await handle_memory_ask(ask)

    cited = set(out.memory_keys or [])
    hits = [h for h in out.results if h.memory_key in cited] or list(out.results)
    evidence = [
        BriefEvidence(
            memory_key=h.memory_key,
            source=getattr(h, "source", "fact") or "fact",
            source_class=getattr(h, "source_class", "") or "",
            verified=bool(getattr(h, "verified", False)),
            when=(getattr(h, "temporal_anchor", "") or getattr(h, "valid_at", "") or ""),
            session_id=getattr(h, "session_id", "") or "",
            excerpt=(getattr(h, "content_excerpt", "") or "")[:_EXCERPT_CAP],
        )
        for h in hits[: input.top_k]
    ]

    unknowns = [out.abstain_reason] if out.abstain and out.abstain_reason else []

    next_reads = _dedupe(
        [f"session:{s}" for s in (out.cited_sessions or [])]
        + [f"search: {u}" for u in unknowns[:3]]
    )

    return MemoryBriefOutput(
        query=input.query,
        answer=out.answer,
        abstained=bool(out.abstain),
        grounding=getattr(out, "grounding", "") or "",
        evidence=evidence,
        unknowns=unknowns,
        next_reads=next_reads,
    )
