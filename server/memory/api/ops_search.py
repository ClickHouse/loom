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
from server.memory.app.served_reads import records_served, search_keys
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================

memory_search = Operation(
    name="memory.search",
    input_model=MemorySearchInput,
    output_model=MemorySearchOutput,
    # The keys its answer returns are recorded as `served` (server/memory/app/served_reads.py).
    handler=records_served(search_keys)(handle_memory_search),
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_search_trace = Operation(
    name="memory.search_trace",
    input_model=MemorySearchTraceInput,
    output_model=MemorySearchTraceOutput,
    handler=handle_memory_search_trace,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)


memory_search_trace_log = Operation(
    name="memory.search_trace_log",
    input_model=SearchTraceLogInput,
    output_model=SearchTraceLogOutput,
    handler=handle_memory_search_trace_log,
    idempotent=True,
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
    required_role=Role.VIEWER,
    audit=False,
)
