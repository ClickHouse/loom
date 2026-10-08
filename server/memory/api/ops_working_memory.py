"""Operations over the wire models in `server.memory.api.models_working_memory`."""

from __future__ import annotations

from server.memory.api.models_working_memory import (
    WorkingMemoryAppendInput,
    WorkingMemoryDeleteInput,
    WorkingMemoryDeleteOutput,
    WorkingMemoryGetInput,
    WorkingMemoryGetOutput,
    WorkingMemoryMutationOutput,
    WorkingMemoryRenderInput,
    WorkingMemoryRenderOutput,
    WorkingMemoryReplaceInput,
    WorkingMemorySetInput,
)
from server.memory.app.working_memory import (
    handle_working_memory_append,
    handle_working_memory_delete,
    handle_working_memory_get,
    handle_working_memory_render,
    handle_working_memory_replace,
    handle_working_memory_set,
)
from server.operations.base import Operation, Role

working_memory_get = Operation(
    name="working_memory.get",
    maturity="experimental",
    input_model=WorkingMemoryGetInput,
    output_model=WorkingMemoryGetOutput,
    handler=handle_working_memory_get,
    idempotent=True,
    budget_p99_ms=40,
    required_role=Role.VIEWER,
    audit=False,
)

working_memory_set = Operation(
    name="working_memory.set",
    input_model=WorkingMemorySetInput,
    output_model=WorkingMemoryMutationOutput,
    handler=handle_working_memory_set,
    idempotent=True,
    budget_p99_ms=40,
    required_role=Role.WRITER,
    audit=False,
)

working_memory_append = Operation(
    name="working_memory.append",
    maturity="experimental",
    input_model=WorkingMemoryAppendInput,
    output_model=WorkingMemoryMutationOutput,
    handler=handle_working_memory_append,
    idempotent=False,
    budget_p99_ms=80,
    required_role=Role.WRITER,
    audit=False,
)

working_memory_replace = Operation(
    name="working_memory.replace",
    maturity="experimental",
    input_model=WorkingMemoryReplaceInput,
    output_model=WorkingMemoryMutationOutput,
    handler=handle_working_memory_replace,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.WRITER,
    audit=False,
)

working_memory_delete = Operation(
    name="working_memory.delete",
    maturity="experimental",
    input_model=WorkingMemoryDeleteInput,
    output_model=WorkingMemoryDeleteOutput,
    handler=handle_working_memory_delete,
    idempotent=True,
    budget_p99_ms=40,
    required_role=Role.WRITER,
    audit=False,
)

working_memory_render = Operation(
    name="working_memory.render",
    input_model=WorkingMemoryRenderInput,
    output_model=WorkingMemoryRenderOutput,
    handler=handle_working_memory_render,
    idempotent=True,
    budget_p99_ms=60,
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS: list[Operation] = [
    working_memory_get,
    working_memory_set,
    working_memory_append,
    working_memory_replace,
    working_memory_delete,
    working_memory_render,
]
