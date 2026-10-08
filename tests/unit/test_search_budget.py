"""Unit tests for the bounded recursive search-fallback cascade.

The three fallback legs of memory.search (iterative-judge retry, HyDE
auto-fallback, query-rewrite RRF) each re-invoke handle_memory_search.
They are collectively bounded by one shared SearchBudget threaded
through the recursive calls via MemorySearchInput._search_budget.

These are CH-free: they exercise the budget arithmetic, the
mint-once/inherit-on-recursion contract, the private-attr survival
through model_copy (the mechanism that makes the budget shared), and the
new MemorySearchOutput telemetry fields. The full cascade behaviour
against a live ClickHouse + LLM is covered by the integration suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


# ============================================================
# SearchBudget arithmetic
# ============================================================


def test_budget_round_cap_default_one_extra_round() -> None:
    from server.memory.app.search.budget import SearchBudget

    b = SearchBudget(max_rounds=1, max_wall_ms=10_000)
    # First fallback round is allowed; the second is denied (cap=1), which
    # is the "default <=1 extra round" the plan requires.
    assert b.try_spend_round() is True
    assert b.try_spend_round() is False
    assert b.rounds_used == 1
    assert b.exhausted is True


def test_budget_zero_rounds_denies_all_fallbacks() -> None:
    from server.memory.app.search.budget import SearchBudget

    b = SearchBudget(max_rounds=0, max_wall_ms=10_000)
    assert b.try_spend_round() is False
    assert b.rounds_used == 0
    assert b.exhausted is True


def test_budget_wall_clock_denies_round_even_with_rounds_left() -> None:
    from server.memory.app.search.budget import SearchBudget

    # Rounds available but wall-clock already spent -> denied.
    b = SearchBudget(max_rounds=5, max_wall_ms=0)
    assert b.try_spend_round() is False
    assert b.exhausted is True
    # No round was consumed since the leg never fired.
    assert b.rounds_used == 0


def test_budget_allows_multiple_rounds_when_configured() -> None:
    from server.memory.app.search.budget import SearchBudget

    b = SearchBudget(max_rounds=3, max_wall_ms=10_000)
    assert [b.try_spend_round() for _ in range(4)] == [True, True, True, False]
    assert b.rounds_used == 3
    assert b.exhausted is True


def test_budget_denies_fallback_when_llm_work_exceeds_cap() -> None:
    from server.memory.app.search.budget import SearchBudget

    b = SearchBudget(max_rounds=3, max_wall_ms=10_000, max_llm_calls=1)
    assert b.try_spend_fallback(llm_calls=1, vector_queries=1) is True
    assert b.try_spend_fallback(llm_calls=1, vector_queries=1) is False
    assert b.rounds_used == 1
    assert b.llm_calls_used == 1
    assert b.exhausted is True


def test_budget_denies_fallback_atomically() -> None:
    from server.memory.app.search.budget import SearchBudget

    b = SearchBudget(max_rounds=1, max_wall_ms=10_000, max_vector_queries=1)
    assert b.try_spend_fallback(llm_calls=1, vector_queries=2) is False
    assert b.rounds_used == 0
    assert b.llm_calls_used == 0
    assert b.vector_queries_used == 0


# ============================================================
# Mint-once / inherit-on-recursion contract
# ============================================================


def test_ensure_budget_mints_from_settings_on_top_level_call() -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.budget import SearchBudget, _ensure_search_budget
    from server.settings import settings

    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    # Top-level call: no budget attached yet -> a fresh one minted from
    # the settings defaults.
    budget = _ensure_search_budget(inp)
    assert isinstance(budget, SearchBudget)
    assert budget.max_rounds == settings.memory_search_fallback_max_rounds
    assert budget.max_wall_ms == settings.memory_search_fallback_max_wall_ms
    assert budget.rounds_used == 0


def test_ensure_budget_inherited_through_recursive_model_copy() -> None:
    """The budget survives model_copy as the SAME mutable instance, so the
    recursive fallback calls share one round/wall-clock accounting object.

    This is the core mechanism: collapse three independent recursion
    guards into one bound.
    """
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.budget import _ensure_search_budget

    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    budget = _ensure_search_budget(inp)

    # Simulate a fallback leg re-attaching the budget to a recursive input.
    retry_input = inp.model_copy(update={"_search_budget": budget, "hyde": True})
    # The recursive call resolves the same instance, not a fresh mint.
    assert _ensure_search_budget(retry_input) is budget

    # And it survives a second model_copy (e.g. query sanitization on the
    # recursive call) so accounting is not silently reset mid-cascade.
    twice = retry_input.model_copy(update={"query": "rephrased"})
    assert _ensure_search_budget(twice) is budget


def test_shared_budget_bounds_the_whole_cascade() -> None:
    """Spending a round on one leg leaves the next leg (a recursive call
    sharing the same budget) with no round left at the default cap of 1.
    """
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.budget import _ensure_search_budget

    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    budget = _ensure_search_budget(inp)
    assert budget.max_rounds == 1  # default

    # Leg A fires (e.g. iterative judge) -> consumes the only round.
    assert budget.try_spend_round() is True
    # A recursive call inherits the exhausted budget...
    retry_input = inp.model_copy(update={"_search_budget": budget})
    inherited = _ensure_search_budget(retry_input)
    # ...so leg B (e.g. HyDE inside the recursion) is denied.
    assert inherited.try_spend_round() is False
    assert inherited.exhausted is True


# ============================================================
# Output telemetry surface
# ============================================================


def test_budget_is_private_not_in_wire_schema() -> None:
    """The shared budget is an internal recursion vehicle, not part of the
    request contract — it must not leak into the serialized model."""
    from server.memory.api.models_search import MemorySearchInput

    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    assert "_search_budget" not in inp.model_dump()
    assert "_search_budget" in inp.__private_attributes__


# ============================================================
# Telemetry: llm_calls_total is authoritative across recursive frames
# ============================================================


def test_llm_calls_total_counts_metered_not_only_reserved() -> None:
    """llm_calls_total reports EVERY LLM call the cascade metered (enrichment,
    HyDE, rerank, judge, rewrite, synonyms), not just the fallback-reserved
    slice. The reserved counter (`fallback_llm_calls_used`) only tracks the
    optional try_spend legs; capacity planning must read the metered total."""
    from server.memory.app.search.budget import SearchBudget
    from server.memory.app.search.telemetry import _actual_work_payload

    b = SearchBudget(max_rounds=1, max_wall_ms=10_000, max_llm_calls=2)
    # A plan-path call (enrichment) is metered but never reserved.
    b.note_llm_call(1)
    # A fallback leg both reserves AND meters its call.
    assert b.try_spend_llm(1) is True
    b.note_llm_call(1)

    out = _actual_work_payload({}, b)
    # Reserved slice = 1 (only the try_spend leg); metered total = 2.
    assert out["fallback_llm_calls_used"] == 1
    assert out["llm_calls_total"] == 2


def test_llm_calls_total_propagates_from_recursive_fallback_frame() -> None:
    """A fallback frame's rewrite/judge LLM calls must surface in the
    TOP-level llm_calls_total. The mechanism is the shared budget: the
    recursive input carries the same SearchBudget instance via model_copy, so
    metered calls inside the recursion increment the object the top frame
    reads. Without this the fallback-frame spend would be invisible to capacity
    planning (the per-stage counters in actual_work are frame-local and lost;
    only the shared metered total catches the recursive spend)."""
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.budget import SearchBudget, _ensure_search_budget
    from server.memory.app.search.telemetry import _actual_work_payload

    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    top_budget = _ensure_search_budget(inp)
    assert isinstance(top_budget, SearchBudget)

    # Top frame meters its own enrichment call.
    top_budget.note_llm_call(1)

    # Fallback leg re-attaches the SAME budget to the recursive input.
    retry_input = inp.model_copy(update={"_search_budget": top_budget})
    recursive_budget = _ensure_search_budget(retry_input)
    assert recursive_budget is top_budget  # shared, not re-minted

    # The recursive frame meters its own rewrite + judge calls.
    recursive_budget.note_llm_call(1)  # query-rewrite generation
    recursive_budget.note_llm_call(1)  # iterative judge

    # Back at the TOP frame, the authoritative total includes all three.
    out = _actual_work_payload({}, top_budget)
    assert out["llm_calls_total"] == 3
