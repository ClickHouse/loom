"""Operations over the wire models in `server.memory.api.models_move`."""

from __future__ import annotations

from server.memory.api.models_move import MemoryMoveInput, MemoryMoveOutput
from server.memory.app.move import handle_memory_move
from server.operations.base import Operation, Role

memory_move = Operation(
    name="memory.move",
    input_model=MemoryMoveInput,
    output_model=MemoryMoveOutput,
    handler=handle_memory_move,
    # A second move of the same from_key answers 409 ALREADY_REDIRECTED.
    idempotent=False,
    # One memory.set (its budget is 200) plus the redirect version, its mirror
    # restamp and the chunk tombstones.
    budget_p99_ms=400,
    required_role=Role.WRITER,
    audit=True,
)
