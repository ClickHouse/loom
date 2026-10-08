"""Recursive search-fallback budget for memory.search."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from server.memory.api.models_search import MemorySearchInput


@dataclass
class SearchBudget:
    """Shared budget bounding the recursive search-fallback cascade.

    The fallback legs (the iterative-judge retry and the read-your-writes
    re-reads) re-invoke `handle_memory_search`. Created once
    on the top-level call and threaded through every recursive call via
    the private `MemorySearchInput._search_budget` attr (survives
    `model_copy`), so the same mutable instance accounts the whole
    cascade. The first-pass retrieve is never gated; only the optional
    fallback legs are.
    """

    max_rounds: int = 1
    max_wall_ms: int = 4000
    max_llm_calls: int = 2
    max_vector_queries: int = 12
    rounds_used: int = 0
    llm_calls_used: int = 0
    vector_queries_used: int = 0
    # Counts EVERY LLM call the cascade issues (plan + fallback), not just
    # the reserved fallback work — feeds the retrieval_work payload.
    llm_calls_metered: int = 0
    exhausted: bool = False
    # Request-scoped reuse of deterministic plan work (the derived-aggregate
    # and temporal-diff legs) across recursive frames.
    plan_cache: dict[Any, Any] = field(default_factory=dict)
    _started: float = field(default_factory=time.monotonic)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def wall_left_s(self) -> float:
        """Wall-clock budget left in seconds; zero or negative once it is spent."""
        return (self.max_wall_ms - self.elapsed_ms()) / 1000.0

    def remaining_wall_s(self) -> float:
        """Wall-clock budget left, as a wait_for timeout.

        Floored at 0.1s so an already-admitted recursion gets a non-zero
        window rather than a guaranteed TimeoutError. Deciding WHETHER to
        recurse reads `wall_left_s`: this floor never reads as spent.
        """
        return max(0.1, self.wall_left_s())

    def note_llm_call(self, count: int = 1) -> None:
        """Record completed plan LLM work for telemetry. Never denies."""
        self.llm_calls_metered += max(0, int(count))

    def try_spend_round(self) -> bool:
        """Reserve one fallback round; True if allowed.

        Denied (marks exhausted) at the round cap or once the wall-clock
        budget is spent.
        """
        if self.rounds_used >= self.max_rounds:
            self.exhausted = True
            return False
        if self.elapsed_ms() >= self.max_wall_ms:
            self.exhausted = True
            return False
        self.rounds_used += 1
        return True

    def try_spend_llm(self, count: int = 1) -> bool:
        """Reserve optional LLM fallback work."""
        count = max(0, int(count))
        if count == 0:
            return True
        if self.elapsed_ms() >= self.max_wall_ms:
            self.exhausted = True
            return False
        if self.llm_calls_used + count > self.max_llm_calls:
            self.exhausted = True
            return False
        self.llm_calls_used += count
        return True

    def fallback_affordable(
        self, *, rounds: int = 1, llm_calls: int = 0, vector_queries: int = 0
    ) -> bool:
        """Whether `try_spend_fallback` with these units (plus `llm_calls` of judging) would
        succeed now; reserves nothing. A leg that must pay an LLM call to decide whether
        to fall back asks this first, so a refused round never costs the call."""
        return (
            self.elapsed_ms() < self.max_wall_ms
            and self.rounds_used + max(0, int(rounds)) <= self.max_rounds
            and self.llm_calls_used + max(0, int(llm_calls)) <= self.max_llm_calls
            and self.vector_queries_used + max(0, int(vector_queries)) <= self.max_vector_queries
        )

    def try_spend_fallback(
        self,
        *,
        rounds: int = 1,
        llm_calls: int = 0,
        vector_queries: int = 0,
    ) -> bool:
        """Atomically reserve a fallback leg's round and work units."""
        rounds = max(0, int(rounds))
        llm_calls = max(0, int(llm_calls))
        vector_queries = max(0, int(vector_queries))
        if self.elapsed_ms() >= self.max_wall_ms:
            self.exhausted = True
            return False
        if self.rounds_used + rounds > self.max_rounds:
            self.exhausted = True
            return False
        if self.llm_calls_used + llm_calls > self.max_llm_calls:
            self.exhausted = True
            return False
        if self.vector_queries_used + vector_queries > self.max_vector_queries:
            self.exhausted = True
            return False
        self.rounds_used += rounds
        self.llm_calls_used += llm_calls
        self.vector_queries_used += vector_queries
        return True


def _ensure_search_budget(input: MemorySearchInput) -> SearchBudget:
    """Return the cascade budget for this query, creating it once.

    Top-level calls have no budget yet → mint one from settings. Recursive
    fallback calls inherit the existing instance via the private attr, so
    accounting is shared across the whole cascade.
    """
    budget = getattr(input, "_search_budget", None)
    if isinstance(budget, SearchBudget):
        return budget
    from server.settings import settings as _settings

    return SearchBudget(
        max_rounds=int(_settings.memory_search_fallback_max_rounds),
        max_wall_ms=int(_settings.memory_search_fallback_max_wall_ms),
        max_llm_calls=int(_settings.memory_search_fallback_max_llm_calls),
        max_vector_queries=int(_settings.memory_search_fallback_max_vector_queries),
    )
