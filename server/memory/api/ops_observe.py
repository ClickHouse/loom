"""Operations over the wire models in `server.memory.api.models_observe`."""

from __future__ import annotations

from server.memory.api.models_observe import (
    ObserveCrystallizeInput,
    ObserveCrystallizeOutput,
    ObserveRecordInput,
    ObserveRecordOutput,
)
from server.memory.app.observe import (
    handle_observe_crystallize,
    handle_observe_record,
)
from server.operations.base import Operation, Role

observe_record = Operation(
    name="observe.record",
    input_model=ObserveRecordInput,
    output_model=ObserveRecordOutput,
    handler=handle_observe_record,
    # Re-fired hook for the same tool call collapses onto the same RMT key.
    idempotent=True,
    budget_p99_ms=150,  # hot path: single insert, no LLM/embedding
    required_role=Role.WRITER,
    audit=False,
)

observe_crystallize = Operation(
    name="observe.crystallize",
    input_model=ObserveCrystallizeInput,
    output_model=ObserveCrystallizeOutput,
    handler=handle_observe_crystallize,
    idempotent=True,
    budget_p99_ms=2000,
    required_role=Role.WRITER,
    audit=False,
)


OPERATIONS: list[Operation] = [observe_record, observe_crystallize]
