"""Operations over the wire models in `server.memory.api.models_demonstrate`."""

from __future__ import annotations

from server.memory.api.models_demonstrate import MemoryDemonstrateInput, MemoryDemonstrateOutput
from server.memory.app.demonstrate import handle_memory_demonstrate
from server.operations.base import Operation, Role

memory_demonstrate = Operation(
    name="memory.demonstrate",
    maturity="experimental",
    input_model=MemoryDemonstrateInput,
    output_model=MemoryDemonstrateOutput,
    handler=handle_memory_demonstrate,
    idempotent=True,
    budget_p99_ms=400,
    required_role=Role.VIEWER,
    audit=False,
)

OPERATIONS: list[Operation] = [memory_demonstrate]
