"""Operations over the wire models in `server.memory.api.models_graph`."""

from __future__ import annotations

from server.memory.api.models_graph import (
    MemoryAssignCategoryInput,
    MemoryAssignCategoryOutput,
    MemoryDedupCollapseInput,
    MemoryDedupCollapseOutput,
    MemoryDedupListInput,
    MemoryDedupListOutput,
    MemoryDedupResolveInput,
    MemoryDedupResolveOutput,
    MemoryLineageInput,
    MemoryLineageOutput,
    MemoryLinkInput,
    MemoryLinkOutput,
    MemoryRelatedInput,
    MemoryRelatedOutput,
)
from server.memory.app.dedup import (
    handle_memory_dedup_collapse,
    handle_memory_dedup_list,
    handle_memory_dedup_resolve,
    handle_memory_lineage,
    handle_memory_related,
)
from server.memory.app.ops import (
    handle_memory_assign_category,
    handle_memory_link,
)
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================

memory_related = Operation(
    name="memory.related",
    maturity="experimental",
    input_model=MemoryRelatedInput,
    output_model=MemoryRelatedOutput,
    handler=handle_memory_related,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_lineage = Operation(
    name="memory.lineage",
    maturity="experimental",
    input_model=MemoryLineageInput,
    output_model=MemoryLineageOutput,
    handler=handle_memory_lineage,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_dedup_list = Operation(
    name="memory.dedup_list",
    input_model=MemoryDedupListInput,
    output_model=MemoryDedupListOutput,
    handler=handle_memory_dedup_list,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=False,
)

memory_dedup_resolve = Operation(
    name="memory.dedup_resolve",
    input_model=MemoryDedupResolveInput,
    output_model=MemoryDedupResolveOutput,
    handler=handle_memory_dedup_resolve,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)

memory_dedup_collapse = Operation(
    name="memory.dedup_collapse",
    maturity="experimental",
    input_model=MemoryDedupCollapseInput,
    output_model=MemoryDedupCollapseOutput,
    handler=handle_memory_dedup_collapse,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

memory_assign_category = Operation(
    name="memory.assign_category",
    maturity="experimental",
    input_model=MemoryAssignCategoryInput,
    output_model=MemoryAssignCategoryOutput,
    handler=handle_memory_assign_category,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

memory_link = Operation(
    name="memory.link",
    input_model=MemoryLinkInput,
    output_model=MemoryLinkOutput,
    handler=handle_memory_link,
    idempotent=False,
    required_role=Role.WRITER,
    audit=True,
)
