"""Operations over the wire models in `server.memory.api.models_context`."""

from __future__ import annotations

from server.memory.api.models_context import ContextPackInput, ContextPackOutput
from server.memory.app.context_pack import handle_context_pack
from server.operations.base import Operation, Role

context_pack = Operation(
    name="context.pack",
    input_model=ContextPackInput,
    output_model=ContextPackOutput,
    handler=handle_context_pack,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS = [context_pack]
