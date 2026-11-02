"""loom_search's memory leg under a code scope: the home search, plus the
decision memories recorded in the scoped repository's trunk code namespace.

The repository namespace arrives already gated (`repo_namespace_in_scope`,
server/code/app/repo_scope.py); `handle_memory_search` does not authorize.
The decision search is one extra in-process pass, LLM-free and marked as
merged into the home search, and its hits join the home hits by similarity.
`search_sections` renders the reader block from the final section.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.settings import settings

log = logging.getLogger("loom.loom_search")


async def search_repo_decisions(
    mem_input: MemorySearchInput, repo_namespace: str
) -> MemorySearchOutput | None:
    """The decision memories in `repo_namespace` that match `mem_input`'s
    query; None when there is nothing to add, when the search abstains, or
    on any failure. The read scope the token allows is pinned inside the
    search, as for the base search."""
    from server.memory.app.search import handle_memory_search

    # The base search already reads its own namespace.
    if repo_namespace == mem_input.namespace or not mem_input.query.strip():
        return None
    decision_input = MemorySearchInput(
        org=mem_input.org,
        namespace=repo_namespace,
        namespace_scope="this",
        agent="*",
        memory_type="decision",
        # The fact legs only: session digests do not filter on memory_type.
        legs=["memory"],
        query=mem_input.query,
        top_k=mem_input.top_k,
        tags=mem_input.tags,
        # An excerpt each, as loom_search serves; memory.get reads a whole body.
        bodies="excerpt",
        floor_mode=mem_input.floor_mode,
        quality_filter=mem_input.quality_filter,
        # Hits are placed by similarity: one embedding, no model call. Pinned
        # here so the namespace's retrieval defaults and rerank config cannot
        # turn them back on.
        retrieval_budget="fast",
        search_mode="semantic",
        hyde=False,
        rerank="none",
    )
    # The parent serves these hits: this pass writes no trace, exposure,
    # recall credit or page of its own (`credit_merged_hits` runs after the merge).
    decision_input._merged_into_parent = True
    try:
        out = await asyncio.wait_for(
            handle_memory_search(decision_input), timeout=settings.knowledge_repo_docs_timeout_s
        )
    except Exception as e:  # noqa: BLE001 — an additive leg never fails the caller
        log.info(
            "repo decisions search on %s/%s did not answer: %s", mem_input.org, repo_namespace, e
        )
        return None
    # Only decision rows: no computed row, digest or entity summary from this pass.
    decisions = [h for h in out.results if h.memory_type == "decision" and h.source == "fact"]
    if out.abstain or not decisions:
        return None
    for h in decisions:
        if not h.namespace:
            h.namespace = repo_namespace
        # Another namespace's row, as the fan-out marks one.
        h.score_components["foreign"] = 1.0
    out.results = decisions
    return out


def _is_computed_row(h: _MemorySearchHit) -> bool:
    """A computed row `memory.search` pins first and serves above `top_k`
    (a derived aggregate or a date difference)."""
    return h.source in ("derived", "computed")


def merge_memory_hits(
    base: list[_MemorySearchHit], extra: list[_MemorySearchHit], home_ns: str, top_k: int
) -> tuple[list[_MemorySearchHit], list[_MemorySearchHit]]:
    """One memory list and the `extra` hits added to it. `base` keeps its
    own order (rank order, not always falling similarity); each `extra` hit
    goes after the last base hit whose similarity is at least its own (the
    rank scores of two searches do not compare), so it never displaces a
    home hit that matched better. One hit per (namespace, memory_key), cut
    at `top_k` (a computed row does not count)."""
    seen = {(h.namespace or home_ns, h.memory_key) for h in base}
    # slots[i]: the extra hits that go after the first i base hits
    slots: list[list[_MemorySearchHit]] = [[] for _ in range(len(base) + 1)]
    for h in sorted(extra, key=lambda x: x.similarity, reverse=True):
        key = (h.namespace or home_ns, h.memory_key)
        if key in seen:
            continue
        seen.add(key)
        after = max((i + 1 for i, b in enumerate(base) if b.similarity >= h.similarity), default=0)
        slots[after].append(h)
    merged: list[tuple[_MemorySearchHit, bool]] = [(h, True) for h in slots[0]]
    for i, b in enumerate(base):
        merged.append((b, False))
        merged.extend((h, True) for h in slots[i + 1])
    out: list[_MemorySearchHit] = []
    added: list[_MemorySearchHit] = []
    kept = 0
    for h, from_extra in merged:
        if not _is_computed_row(h):
            if kept == top_k:
                continue
            kept += 1
        out.append(h)
        if from_extra:
            added.append(h)
    return out, added


async def memory_with_repo_decisions(
    mem_input: MemorySearchInput, *, repo_namespace: str
) -> MemorySearchOutput:
    """`loom_search`'s memory leg: the memory search, and — when the code
    scope names a repository the caller may read (`repo_namespace`) — its
    decisions, merged into the one section. Raises only what the base search
    raises."""
    from server.memory.app.search import handle_memory_search
    from server.memory.app.search.telemetry import credit_merged_hits

    if not repo_namespace:
        return await handle_memory_search(mem_input)
    base, decisions = await asyncio.gather(
        handle_memory_search(mem_input), search_repo_decisions(mem_input, repo_namespace)
    )
    if decisions is None:
        return base
    # The role ceiling the base search applied bounds the merged list too.
    top_k = base.top_k_ceiling or mem_input.top_k
    hits, added = merge_memory_hits(base.results, decisions.results, mem_input.namespace, top_k)
    if not added:
        return base
    # Credited as the base search credits its own: for the hits it hands on,
    # before the section's floors and answer-first rules, with the input the
    # decision pass ran (set on every pass that searched).
    settled = decisions._settled_input
    if settled is not None:
        await credit_merged_hits(settled, added)
    update: dict[str, Any] = {
        "results": hits,
        # At least what is served; the "more on a later page" text follows next_page.
        "candidates_total": max(base.candidates_total, len(hits)),
        "namespaces_searched": list(dict.fromkeys([*base.namespaces_searched, repo_namespace])),
        "reader_block": "",
    }
    if base.abstain:
        # The decisions answer where the home search could not: the section
        # carries the decision search's own calibration.
        update.update(
            abstain=False,
            abstain_reason="",
            abstain_code="",
            retrieval_confidence=decisions.retrieval_confidence,
            retrieval_risk=decisions.retrieval_risk,
            route_agreement=decisions.route_agreement,
            calibration_explanation=decisions.calibration_explanation,
        )
    return base.model_copy(update=update)
