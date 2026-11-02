"""memory.demonstrate handler."""

from __future__ import annotations

from server.memory.api.models_demonstrate import (
    DemonstrationRow,
    MemoryDemonstrateInput,
    MemoryDemonstrateOutput,
)
from server.memory.api.models_search import MemorySearchInput


async def handle_memory_demonstrate(input: MemoryDemonstrateInput) -> MemoryDemonstrateOutput:
    """Nearest memories tagged as examples → labels from their tags → a
    similarity-weighted vote and a few-shot block. One search (semantic, tag-filtered,
    twice `k` examples so unlabelled ones do not starve the vote, one hit per example)
    under `include_tags`, so each hit carries its own row's tags, read in the namespace
    it came from (`all_mine` spans several)."""
    from server.memory.app.search import handle_memory_search
    from server.memory.app.search.scoring import one_per_key, per_key_fetch
    from server.memory.domain import demonstrations as svc

    want = min(200, input.k * 2)
    search = await handle_memory_search(
        MemorySearchInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            query=input.query,
            top_k=per_key_fetch(want),
            bodies="full",  # the over-fetch must not drop the examples to excerpts
            tags=[input.example_tag],
            search_mode="semantic",
            namespace_scope=input.namespace_scope,
            auto_touch=False,
            include_tags=True,
        )
    )
    # one vote per example, however many sessions it was written in
    hits = one_per_key(list(search.results or []), input.namespace, want)
    if not hits:
        return MemoryDemonstrateOutput(
            query=input.query,
            demonstrations=[],
            abstain=True,
            abstain_reason="no_examples",
            examined=0,
            labelled=0,
        )
    demos: list[svc.Demonstration] = []
    for h in hits:
        label = svc.label_of(list(getattr(h, "tags", None) or []), input.label_prefix)
        if not label:
            continue
        demos.append(
            svc.Demonstration(
                memory_key=str(h.memory_key),
                text=str(getattr(h, "content_excerpt", "") or ""),
                label=label,
                similarity=float(getattr(h, "similarity", 0.0) or 0.0),
            )
        )
        if len(demos) >= input.k:
            break
    v = svc.vote(demos)
    abstain = not demos or v.margin < input.min_margin
    reason = "no_labelled_examples" if not demos else ("low_margin" if abstain else "")
    return MemoryDemonstrateOutput(
        query=input.query,
        demonstrations=[
            DemonstrationRow(
                memory_key=d.memory_key, text=d.text, label=d.label, similarity=d.similarity
            )
            for d in demos
        ],
        votes=v.votes,
        predicted_label=v.predicted,
        margin=v.margin,
        abstain=abstain,
        abstain_reason=reason,
        few_shot_block=svc.few_shot_block(demos, max_chars=input.max_block_chars),
        examined=len(hits),
        labelled=len(demos),
    )
