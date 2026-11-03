"""The `memory.inspect` operation; wire models are in models_inspect.py."""

from __future__ import annotations

from server.memory.api.models_inspect import MemoryInspectInput, MemoryInspectOutput
from server.memory.app.inspect import handle_memory_inspect
from server.operations.base import Operation, Role

memory_inspect = Operation(
    name="memory.inspect",
    maturity="experimental",
    input_model=MemoryInspectInput,
    output_model=MemoryInspectOutput,
    handler=handle_memory_inspect,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)
