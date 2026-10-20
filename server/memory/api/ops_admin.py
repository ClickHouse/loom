"""Operations over the wire models in `server.memory.api.models_admin`."""

from __future__ import annotations

from server.memory.api.models_admin import (
    ConsolidationsListInput,
    ConsolidationsListOutput,
    MemoryAgentsListInput,
    MemoryAgentsListOutput,
    MemoryConsolidateInput,
    MemoryConsolidateOutput,
    MemoryHealthInput,
    MemoryHealthOutput,
    MemoryReindexInput,
    MemoryReindexOutput,
    MemoryResolveOrphanDuplicatesInput,
    MemoryResolveOrphanDuplicatesOutput,
)
from server.memory.app.consolidate import handle_consolidations_list, handle_memory_consolidate
from server.memory.app.health import handle_memory_health
from server.memory.app.ops import (
    handle_memory_agents_list,
    handle_memory_reindex,
    handle_memory_resolve_orphan_duplicates,
)
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================

memory_consolidate = Operation(
    name="memory.consolidate",
    input_model=MemoryConsolidateInput,
    output_model=MemoryConsolidateOutput,
    handler=handle_memory_consolidate,
    idempotent=False,
    required_role=Role.WRITER,
    audit=True,
)

consolidations_list = Operation(
    name="consolidations.list",
    maturity="experimental",
    input_model=ConsolidationsListInput,
    output_model=ConsolidationsListOutput,
    handler=handle_consolidations_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_health = Operation(
    name="memory.health",
    input_model=MemoryHealthInput,
    output_model=MemoryHealthOutput,
    handler=handle_memory_health,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_agents_list = Operation(
    name="memory.agents_list",
    input_model=MemoryAgentsListInput,
    output_model=MemoryAgentsListOutput,
    handler=handle_memory_agents_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_reindex = Operation(
    name="memory.reindex",
    maturity="experimental",
    input_model=MemoryReindexInput,
    output_model=MemoryReindexOutput,
    handler=handle_memory_reindex,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)

memory_resolve_orphan_duplicates = Operation(
    name="memory.resolve_orphan_duplicates",
    input_model=MemoryResolveOrphanDuplicatesInput,
    output_model=MemoryResolveOrphanDuplicatesOutput,
    handler=handle_memory_resolve_orphan_duplicates,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)
