"""Operations over the wire models in `server.memory.api.models_for_file`."""

from __future__ import annotations

from server.memory.api.models_for_file import MemoryForFileInput, MemoryForFileOutput
from server.memory.app.for_file import handle_memory_for_file
from server.memory.app.served_reads import for_file_keys, records_served
from server.operations.base import Operation, Role

memory_for_file = Operation(
    name="memory.for_file",
    input_model=MemoryForFileInput,
    output_model=MemoryForFileOutput,
    handler=records_served(for_file_keys)(handle_memory_for_file),
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)
