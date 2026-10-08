"""Operations over the wire models in `server.memory.api.models_brief`."""

from __future__ import annotations

from server.memory.api.models_brief import MemoryBriefInput, MemoryBriefOutput
from server.memory.app.brief import (
    handle_memory_brief,
)
from server.operations.base import Operation, Role

memory_brief = Operation(
    name="memory.brief",
    input_model=MemoryBriefInput,
    output_model=MemoryBriefOutput,
    handler=handle_memory_brief,
    idempotent=True,
    budget_p99_ms=4000,
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS = [memory_brief]
