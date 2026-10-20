"""Operations over the wire models in `server.memory.api.models_evidence_transfer`."""

from __future__ import annotations

from server.memory.api.models_evidence_transfer import (
    MemoryExportEvidenceInput,
    MemoryExportEvidenceOutput,
    MemoryImportEvidenceInput,
    MemoryImportEvidenceOutput,
)
from server.memory.app.evidence_transfer import (
    handle_memory_export_evidence,
    handle_memory_import_evidence,
)
from server.operations.base import Operation, Role

memory_export_evidence = Operation(
    name="memory.export_evidence",
    input_model=MemoryExportEvidenceInput,
    output_model=MemoryExportEvidenceOutput,
    handler=handle_memory_export_evidence,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_import_evidence = Operation(
    name="memory.import_evidence",
    input_model=MemoryImportEvidenceInput,
    output_model=MemoryImportEvidenceOutput,
    handler=handle_memory_import_evidence,
    idempotent=False,
    required_role=Role.WRITER,
    audit=True,
)
