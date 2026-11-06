"""Operations over the wire models in `server.memory.api.models_ask`."""

from __future__ import annotations

from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput
from server.memory.app.ask.handler import handle_memory_ask
from server.memory.app.served_reads import ask_keys, records_served
from server.operations.base import Operation, Role

memory_ask = Operation(
    name="memory.ask",
    input_model=MemoryAskInput,
    output_model=MemoryAskOutput,
    # The keys its answer returns are recorded as `served` (server/memory/app/served_reads.py).
    handler=records_served(ask_keys)(handle_memory_ask),
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)
