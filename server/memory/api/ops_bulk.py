"""Operations over the wire models in `server.memory.api.models_bulk`."""

from __future__ import annotations

from server.memory.api.models_bulk import (
    MemoryBulkGetInput,
    MemoryBulkGetOutput,
    MemoryBulkSetInput,
    MemoryBulkSetOutput,
    MemoryExtractionRetryMineInput,
    MemoryExtractionRetryMineOutput,
    MemoryExtractionStatusInput,
    MemoryExtractionStatusMineInput,
    MemoryExtractionStatusMineOutput,
    MemoryExtractionStatusOutput,
    MemoryImportCuratedInput,
    MemoryImportCuratedOutput,
    MemorySetFromMessagesInput,
    MemorySetFromMessagesOutput,
)
from server.memory.app.bulk_set import handle_memory_bulk_set
from server.memory.app.curation import handle_memory_import_curated
from server.memory.app.extract.handler import (
    handle_memory_extraction_retry_mine,
    handle_memory_extraction_status,
    handle_memory_extraction_status_mine,
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
    required_role=Role.WRITER,
    audit=False,
)

memory_set_from_messages = Operation(
    name="memory.set_from_messages",
    input_model=MemorySetFromMessagesInput,
    output_model=MemorySetFromMessagesOutput,
    handler=handle_memory_set_from_messages,
    idempotent=False,  # each call re-runs the LLM and emits new versions
    required_role=Role.WRITER,
    audit=False,
)


memory_extraction_status = Operation(
    name="memory.extraction_status",
    input_model=MemoryExtractionStatusInput,
    output_model=MemoryExtractionStatusOutput,
    handler=handle_memory_extraction_status,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

# The caller's own batches by id, at most MAX_OWN_BATCH_IDS: what an agent polls over
# MCP, where the namespace-wide memory.extraction_status is hidden.
memory_extraction_status_mine = Operation(
    name="memory.extraction_status_mine",
    maturity="experimental",
    input_model=MemoryExtractionStatusMineInput,
    output_model=MemoryExtractionStatusMineOutput,
    handler=handle_memory_extraction_status_mine,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

# A retry targets the observed failed version; repeated submissions cannot change
# a newer worker result or create another copy of the queued transcript.
memory_extraction_retry_mine = Operation(
    name="memory.extraction_retry_mine",
    maturity="experimental",
    input_model=MemoryExtractionRetryMineInput,
    output_model=MemoryExtractionRetryMineOutput,
    handler=handle_memory_extraction_retry_mine,
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
)

memory_bulk_get = Operation(
    name="memory.bulk_get",
    input_model=MemoryBulkGetInput,
    output_model=MemoryBulkGetOutput,
    handler=handle_memory_bulk_get,
    idempotent=True,
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
    required_role=Role.WRITER,
    audit=True,
)
