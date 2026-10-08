"""The `cost` operations the registry serves; wire models are in models_cost.py."""

from __future__ import annotations

from server.insights.api.models_cost import (
    BudgetStatusInput,
    BudgetStatusOutput,
    CostAgentSummaryInput,
    CostAgentSummaryOutput,
    CostRecordTurnsInput,
    CostRecordTurnsOutput,
    CostSummaryInput,
    CostSummaryOutput,
)
from server.insights.app.cost import (
    handle_budget_status,
    handle_cost_agent_summary,
    handle_cost_record_turns,
    handle_cost_summary,
)
from server.operations.base import Operation, Role

cost_summary = Operation(
    name="cost.summary",
    input_model=CostSummaryInput,
    output_model=CostSummaryOutput,
    handler=handle_cost_summary,
    idempotent=True,
    budget_p99_ms=200,
    required_role=Role.VIEWER,
    audit=False,
)


cost_budget_status = Operation(
    name="cost.budget_status",
    input_model=BudgetStatusInput,
    output_model=BudgetStatusOutput,
    handler=handle_budget_status,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
)


cost_record_turns = Operation(
    name="cost.record_turns",
    maturity="experimental",
    input_model=CostRecordTurnsInput,
    output_model=CostRecordTurnsOutput,
    handler=handle_cost_record_turns,
    idempotent=True,  # same (session, message_id) rows overwrite
    budget_p99_ms=150,
    required_role=Role.WRITER,
    audit=False,
)


cost_agent_summary = Operation(
    name="cost.agent_summary",
    input_model=CostAgentSummaryInput,
    output_model=CostAgentSummaryOutput,
    handler=handle_cost_agent_summary,
    idempotent=True,
    budget_p99_ms=250,
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS: list[Operation] = [
    cost_summary,
    cost_budget_status,
    cost_record_turns,
    cost_agent_summary,
]
