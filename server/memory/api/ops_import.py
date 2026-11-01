"""Operations over the wire models in `server.memory.api.models_import`."""

from __future__ import annotations

from server.memory.api.models_import import MemoryImportInput, MemoryImportOutput
from server.memory.app.memory_import import handle_memory_import
from server.operations.base import Operation, Role

# Not audited by the transport: the bulk write leaves its own hash-only row, and the
# handler records the category links it writes (a dry run writes neither). The input
# carries item text, which `loom.audit_log` must not hold.
memory_import = Operation(
    name="memory.import",
    maturity="experimental",
    input_model=MemoryImportInput,
    output_model=MemoryImportOutput,
    handler=handle_memory_import,
    # Keys derive from the text: a retry finds its own rows, writes nothing new and
    # records no usage event for them.
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
)
