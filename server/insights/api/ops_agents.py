"""The `agents` operations the registry serves; wire models are in models_agents.py."""

from __future__ import annotations

from server.insights.api.models_agents import (
    AgentsConnectedInput,
    AgentsConnectedOutput,
)
from server.insights.app.agents import handle_agents_connected
from server.operations.base import Operation, Role

agents_connected = Operation(
    name="agents.connected",
    input_model=AgentsConnectedInput,
    output_model=AgentsConnectedOutput,
    handler=handle_agents_connected,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS: list[Operation] = [agents_connected]
