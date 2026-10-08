"""Operations over the wire models in `server.memory.api.models_bulk`."""

from __future__ import annotations

from server.memory.api.models_bulk import (
    MemoryBulkGetInput,
    MemoryBulkGetOutput,
    MemoryBulkSetInput,
    MemoryBulkSetOutput,
    MemoryExtractionStatusInput,
    MemoryExtractionStatusOutput,
    MemoryImportCuratedInput,
    MemoryImportCuratedOutput,
    MemorySetFromMessagesInput,
    MemorySetFromMessagesOutput,
)
from server.memory.app.bulk_set import handle_memory_bulk_set
from server.memory.app.curation import handle_memory_import_curated
from server.memory.app.extract.handler import (
    handle_memory_extraction_status,
    handle_memory_set_from_messages,
)
from server.memory.app.query import handle_memory_bulk_get
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================

memory_bulk_set = Operation(
    name="memory.bulk_set",
    input_model=MemoryBulkSetInput,
    output_model=MemoryBulkSetOutput,
    handler=handle_memory_bulk_set,
    idempotent=True,
    budget_p99_ms=300,  # one CH insert + N embeds
    required_role=Role.WRITER,
    audit=False,
)

memory_set_from_messages = Operation(
    name="memory.set_from_messages",
    input_model=MemorySetFromMessagesInput,
    output_model=MemorySetFromMessagesOutput,
    handler=handle_memory_set_from_messages,
    idempotent=False,  # each call re-runs the LLM and emits new versions
    # LLM extraction dominates (~7K-token prompt); downstream bulk_set adds ~200ms.
    budget_p99_ms=2500,
    required_role=Role.WRITER,
    audit=False,
)


memory_extraction_status = Operation(
    name="memory.extraction_status",
    input_model=MemoryExtractionStatusInput,
    output_model=MemoryExtractionStatusOutput,
    handler=handle_memory_extraction_status,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=False,
)

memory_bulk_get = Operation(
    name="memory.bulk_get",
    input_model=MemoryBulkGetInput,
    output_model=MemoryBulkGetOutput,
    handler=handle_memory_bulk_get,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=False,
)

memory_import_curated = Operation(
    name="memory.import_curated",
    maturity="experimental",
    input_model=MemoryImportCuratedInput,
    output_model=MemoryImportCuratedOutput,
    handler=handle_memory_import_curated,
    idempotent=False,
    # ~50 sections × per-section memory.set; embed-on-write dominates.
    budget_p99_ms=30_000,
    required_role=Role.WRITER,
    audit=True,
)
