"""The synthesis pool's composition rides the result, the
output and the ledger."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.api.models_ask import MemoryAskOutput
from server.memory.app.ask import MemoryAskResult
from server.memory.app.ask.result import pool_composition
from server.observability.event_args import IDENTIFIER_KEYS


def test_pool_composition_counts_facts_and_chunks_not_code_rows() -> None:
    pool = [
        SimpleNamespace(memory_key="user_a", source="memory"),
        SimpleNamespace(memory_key="chunk:s1:3", source="chunk"),
        SimpleNamespace(memory_key="chunk:s2:0", source=""),
        SimpleNamespace(memory_key="code_pr:ns:1", source="code_index"),
        SimpleNamespace(memory_key="summary:s1", source="summary"),
    ]
    assert pool_composition(pool) == (2, 2)
    assert pool_composition([]) == (0, 0)


def test_pool_counts_ride_the_result_the_output_and_the_ledger() -> None:
    r = MemoryAskResult(answer="", source="synthesized")
    assert (r.pool_facts, r.pool_chunks) == (0, 0)
    assert {"pool_facts", "pool_chunks"} <= set(MemoryAskOutput.model_fields)
    assert {"pool_facts", "pool_chunks"} <= set(IDENTIFIER_KEYS)
