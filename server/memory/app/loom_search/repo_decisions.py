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
) -> tuple[MemorySearchOutput, MemorySearchInput] | None:
    """The decision memories in `repo_namespace` that match `mem_input`'s
    query, with the input the search settled on, its `top_k` reset to the
    caller's; None when there is nothing to add, when the search abstains, or
    on any failure. The read scope the token allows is pinned inside the
    search, as for the base search."""
    from server.memory.app.search import handle_memory_search
    from server.memory.app.search.scoring import one_per_key, per_key_fetch

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
        # One item per decision key: ask for enough rows that `top_k` keys survive the trim.
        top_k=per_key_fetch(mem_input.top_k),
        tags=mem_input.tags,
        # A recorded statement fits the stored clip, and its reason comes last: an
        # excerpt would cut it. bodies="none" still serves keys only.
        bodies="none" if mem_input.bodies == "none" else "full",
        # Only rows that clear the floor: padding the section is the home search's job.
        floor_mode="drop",
        # The junk classifier targets captured session rows; a decision is authored,
        # and two decisions under different keys are two decisions however alike.
        quality_filter=False,
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
            handle_memory_search(decision_input), timeout=settings.memory_repo_decisions_timeout_s
        )
    except Exception as e:  # noqa: BLE001 — an additive leg never fails the caller
        log.info(
            "repo decisions search on %s/%s did not answer: %s", mem_input.org, repo_namespace, e
        )
        return None
    # Only decision rows: no computed row, digest or entity summary from this pass.
    decisions = [h for h in out.results if h.memory_type == "decision" and h.source == "fact"]
    decisions = one_per_key(decisions, repo_namespace, mem_input.top_k)
    settled = out._settled_input
    if out.abstain or not decisions or settled is None:
        return None
    for h in decisions:
        if not h.namespace:
            h.namespace = repo_namespace
        # Another namespace's row, as the fan-out marks one.
        h.score_components["foreign"] = 1.0
    out.results = decisions
    # Credited with the window the caller asked for, not the over-fetch.
    return out, settled.model_copy(update={"top_k": mem_input.top_k})


def merge_memory_hits(
    base: list[_MemorySearchHit], extra: list[_MemorySearchHit], home_ns: str
) -> tuple[list[_MemorySearchHit], list[_MemorySearchHit]]:
    """One memory list and the `extra` hits added to it. `base` keeps its
    own order (rank order, not always falling similarity); each `extra` hit
    goes after the last base hit whose similarity is at least its own (the
    rank scores of two searches do not compare). An `extra` hit whose
    (namespace, memory_key) the base or an earlier extra hit holds is left
    out. Nothing is cut: the extra hits are bounded by the caller's
    `top_k` (`search_repo_decisions` keeps one per key), and a base hit pushed
    out would be served on no page."""
    seen = {(h.namespace or home_ns, h.memory_key) for h in base}
    # slots[i]: the extra hits that go after the first i base hits
    slots: list[list[_MemorySearchHit]] = [[] for _ in range(len(base) + 1)]
    added: list[_MemorySearchHit] = []
    for h in sorted(extra, key=lambda x: x.similarity, reverse=True):
        key = (h.namespace or home_ns, h.memory_key)
        if key in seen:
            continue
        seen.add(key)
        after = max((i + 1 for i, b in enumerate(base) if b.similarity >= h.similarity), default=0)
        slots[after].append(h)
        added.append(h)
    out = list(slots[0])
    for i, b in enumerate(base):
        out.append(b)
        out.extend(slots[i + 1])
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
    extra = asyncio.create_task(search_repo_decisions(mem_input, repo_namespace))
    try:
        base = await handle_memory_search(mem_input)
    except BaseException:
        extra.cancel()
        raise
    found = await extra
    if found is None:
        return base
    decisions, settled = found
    hits, added = merge_memory_hits(base.results, decisions.results, mem_input.namespace)
    if not added:
        return base
    # Credited as the base search credits its own: for the hits it hands on,
    # before the section's floors and answer-first rules.
    await credit_merged_hits(settled, added)
    update: dict[str, Any] = {
        "results": hits,
        "candidates_total": base.candidates_total + len(added),
        "namespaces_searched": list(dict.fromkeys([*base.namespaces_searched, repo_namespace])),
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
