"""Operations over the wire models in `server.memory.api.models_search`."""

from __future__ import annotations

from server.memory.api.models_search import (
    MemoryAggregateInput,
    MemoryAggregateOutput,
    MemorySearchInput,
    MemorySearchOutput,
    MemorySearchTraceInput,
    MemorySearchTraceOutput,
    SearchTraceLogInput,
    SearchTraceLogOutput,
)
from server.memory.app.query import handle_memory_aggregate
from server.memory.app.search import (
    handle_memory_search,
    handle_memory_search_trace,
    handle_memory_search_trace_log,
)
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================

memory_search = Operation(
    name="memory.search",
    input_model=MemorySearchInput,
    output_model=MemorySearchOutput,
    handler=handle_memory_search,
    idempotent=True,
    # Covers semantic / hybrid / all_mine fan-out (auto_touch is async, no
    # contribution). Reranker hop is EXCLUDED — the operator endpoint owns its
    # own latency. Rerank usage surfaces as `loom.search.assist{kind="rerank"}`
    # (read off `retrieval_work.inline_rerank_used` / `external_rerank_used`);
    # there is no `loom.rerank_ms` metric.
    budget_p99_ms=200,
    required_role=Role.VIEWER,
    audit=False,
)

memory_search_trace = Operation(
    name="memory.search_trace",
    input_model=MemorySearchTraceInput,
    output_model=MemorySearchTraceOutput,
    handler=handle_memory_search_trace,
    idempotent=True,
    budget_p99_ms=200,
    required_role=Role.VIEWER,
    audit=False,
)


memory_search_trace_log = Operation(
    name="memory.search_trace_log",
    input_model=SearchTraceLogInput,
    output_model=SearchTraceLogOutput,
    handler=handle_memory_search_trace_log,
    idempotent=True,
    budget_p99_ms=200,
    required_role=Role.VIEWER,
    audit=False,
)

memory_aggregate = Operation(
    name="memory.aggregate",
    maturity="experimental",
    input_model=MemoryAggregateInput,
    output_model=MemoryAggregateOutput,
    handler=handle_memory_aggregate,
    idempotent=True,
    # One JSONExtract query over loom.memory FINAL; budget covers large group-by cardinality.
    budget_p99_ms=250,
    required_role=Role.VIEWER,
    audit=False,
)
