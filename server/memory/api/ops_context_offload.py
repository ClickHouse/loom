"""Operations over the wire models in `server.memory.api.models_context_offload`."""

from __future__ import annotations

from server.memory.api.models_context_offload import (
    ContextFetchInput,
    ContextFetchOutput,
    ContextOffloadInput,
    ContextOffloadOutput,
)
from server.memory.app.context_offload import handle_context_fetch, handle_context_offload
from server.operations.base import Operation, Role

context_offload = Operation(
    name="context.offload",
    maturity="experimental",
    input_model=ContextOffloadInput,
    output_model=ContextOffloadOutput,
    handler=handle_context_offload,
    idempotent=True,
    budget_p99_ms=300,
    required_role=Role.WRITER,
    audit=False,
)

context_fetch = Operation(
    name="context.fetch",
    maturity="experimental",
    input_model=ContextFetchInput,
    output_model=ContextFetchOutput,
    handler=handle_context_fetch,
    idempotent=True,
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)

OPERATIONS = [context_offload, context_fetch]
