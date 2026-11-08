"""The assistant operations the registry serves; wire models are in models.py."""

from __future__ import annotations

from server.assistant.api.models import AssistantAskInput, AssistantAskOutput
from server.assistant.app.ask import handle_assistant_ask
from server.operations.base import Operation, Role

assistant_ask = Operation(
    name="assistant.ask",
    maturity="experimental",
    input_model=AssistantAskInput,
    output_model=AssistantAskOutput,
    handler=handle_assistant_ask,
    # Each call spends model budget: never resent.
    idempotent=False,
    required_role=Role.VIEWER,
    audit=False,
)

OPERATIONS: list[Operation] = [assistant_ask]
