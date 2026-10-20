"""Query sanitisation, recipe translation, and enrichment-gate helpers for
memory.search."""

from __future__ import annotations

from server.memory.api.models_search import MemorySearchInput


def _sanitize_agent_query(query: str) -> str:
    """Strip obvious tool/schema/system-prompt contamination from a search query."""

    if not query:
        return query
    noisy_markers = (
        '"inputSchema"',
        '"outputSchema"',
        "tools/call",
        "tools/list",
        "<system_reminder",
        "<user_info>",
        "<git_status>",
        "You are an AI",
        "You are GPT",
    )
    kept: list[str] = []
    for line in query.splitlines():
        stripped = line.strip()
        if any(marker in stripped for marker in noisy_markers):
            continue
        if stripped.startswith(("```json", "```schema")):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    if not cleaned:
        return query.strip()
    # Very long MCP-originated queries are usually pasted transcript or
    # schema context. Keep the user's actual ask at the end, where clients
    # tend to append it after boilerplate.
    if len(cleaned) > 2000:
        cleaned = cleaned[-2000:]
    return cleaned


def _apply_search_recipe(input: MemorySearchInput) -> MemorySearchInput:
    """Translate named recipes into existing lower-level search knobs.

    Recipes supply *defaults*; they do not override the caller. A field the
    caller set explicitly always wins — otherwise `{"recipe": "quality",
    "hyde": false}` silently runs with `hyde=True` and the caller gets no
    signal that half the request was discarded (and a recipe could not be bisected by
    disabling one leg per request). `_field_was_set` reads Pydantic's `model_fields_set`, so
    an explicit value that happens to equal the field default still counts as set.
    """

    recipe = (input.recipe or "").strip()
    if not recipe:
        return input
    updates: dict[str, object] = {}
    if recipe == "fast":
        updates = {"search_mode": "semantic", "hyde": False, "include_top_n_unmatched": 0}
    elif recipe == "balanced":
        updates = {"search_mode": "rrf", "hyde": False}
    elif recipe == "quality":
        updates = {
            "search_mode": "bm25_hybrid",
            "hyde": True,
            "include_top_n_unmatched": max(input.include_top_n_unmatched, 50),
        }
    elif recipe == "verbatim":
        updates = {"search_mode": "lexical", "hyde": False, "alpha": 0.85}
    elif recipe == "code":
        updates = {"search_mode": "bm25_hybrid", "hyde": False, "alpha": 0.75}
    elif recipe == "temporal":
        updates = {"search_mode": "temporal", "hyde": False}
    elif recipe == "graph":
        updates = {"search_mode": "graph", "hyde": False}
    # Drop anything the caller pinned themselves. Note this also makes the
    # `quality` max() above a no-op when include_top_n_unmatched was set
    # explicitly, which is the intended reading: an explicit 0 means 0.
    updates = {k: v for k, v in updates.items() if not _field_was_set(input, k)}
    return input.model_copy(update=updates) if updates else input


def _apply_default_search_mode(input: MemorySearchInput) -> MemorySearchInput:
    """Fill `search_mode` from `memory_search_default_mode` when the caller
    pinned neither the mode nor a recipe.

    The wire default stays "rrf" so the schema the agent reads does not
    change; the deployment decides what an unpinned search runs. Runs after
    `_apply_search_recipe` (a recipe is a caller choice) and before the query
    planner, whose shape rules take the result as their starting mode.
    """
    if _field_was_set(input, "search_mode") or (input.recipe or "").strip():
        return input
    from server.settings import settings

    mode = settings.memory_search_default_mode
    # The planner needs to know the mode was the deployment's choice, not the caller's,
    # whether or not the value changed. A private attr: model_copy would otherwise mark
    # `search_mode` as caller-set.
    if mode == input.search_mode:
        input._search_mode_defaulted = True  # in place: the value did not change
        return input
    return input.model_copy(update={"search_mode": mode, "_search_mode_defaulted": True})


def _field_was_set(input: MemorySearchInput, name: str) -> bool:
    return name in getattr(input, "model_fields_set", set())


def _looks_like_complex_agent_query(query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return False
    if len(q) > 180 or "\n" in q:
        return True
    complex_markers = (
        "compare",
        "versus",
        " vs ",
        "before",
        "after",
        "between",
        "timeline",
        "chronolog",
        "first",
        "latest",
        "recent",
        "last week",
        "last month",
        "yesterday",
        "tomorrow",
        # Relative-past anchors. Time-unit + "ago" forms only — a bare
        # " ago" substring false-positives on "agonizing" / "agora".
        "day ago",
        "days ago",
        "week ago",
        "weeks ago",
        "month ago",
        "months ago",
        "year ago",
        "years ago",
        "hour ago",
        "hours ago",
        "while ago",
        "time ago",
        "how many",
        # Duration questions — the interrogative head, not a free " long".
        "how long",
        "count",
        "sum",
        "average",
        "total",
        "group",
        "every",
        "all ",
        "each ",
        "find all",
        "list all",
    )
    if any(marker in q for marker in complex_markers):
        return True
    # Multiple question/clause boundaries signal decomposition or broad recall.
    return q.count("?") + q.count(";") >= 2


def _should_run_query_enrichment(input: MemorySearchInput) -> bool:
    if input.retrieval_budget == "fast":
        return False
    if input.recipe == "fast":
        return False
    if input.search_mode in {"graph", "temporal"}:
        return True
    if input.recipe in {"quality", "temporal", "graph"}:
        return True
    # observation_date alone is NOT a complexity signal — agents attach it
    # to every query for relative-date anchoring. Gate on query SHAPE so
    # simple dated lookups stay on the cheap path.
    return _looks_like_complex_agent_query(input.query)


def _enrichment_parser_gate_active(input: MemorySearchInput) -> bool:
    """Is the deterministic-parser-first enrichment gate active?

    Active under fast posture only: a confident parser spec is free signal
    the fast path otherwise lacks, and abstention degrades to the bare query (fast
    posture forbids LLM spend). Every other posture keeps the decision
    `_should_run_query_enrichment` drives.
    """
    return _is_fast_posture(input)


def _is_fast_posture(input: MemorySearchInput) -> bool:
    """True when the caller asked for the cheap path (recipe or budget).

    Fast posture means NO optional LLM spend: the iterative judge and
    HyDE auto-fallback stay off so the latency floor is a
    vector query, not an LLM round-trip.
    """
    return input.recipe == "fast" or input.retrieval_budget == "fast"


def _is_llm_free(input: MemorySearchInput) -> bool:
    """True when the read makes no LLM call: under the fast posture, or when the
    caller asked for an LLM-free read (context.pack) whatever its recipe."""
    return _is_fast_posture(input) or input._llm_free
