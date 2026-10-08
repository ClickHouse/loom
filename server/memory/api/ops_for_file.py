"""Operations over the wire models in `server.memory.api.models_for_file`."""

from __future__ import annotations

from server.memory.api.models_for_file import MemoryForFileInput, MemoryForFileOutput
from server.memory.app.for_file import handle_memory_for_file
from server.operations.base import Operation, Role

memory_for_file = Operation(
    name="memory.for_file",
    input_model=MemoryForFileInput,
    output_model=MemoryForFileOutput,
    handler=handle_memory_for_file,
    idempotent=True,
    # One FINAL scan filtered by array predicates;
    # it sits on the PreToolUse path, so the hook bounds it at 3 s anyway.
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)
