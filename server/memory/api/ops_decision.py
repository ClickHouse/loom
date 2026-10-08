"""Operations over the wire models in `server.memory.api.models_decision`."""

from __future__ import annotations

from server.memory.api.models_decision import (
    DecisionChainInput,
    DecisionChainOutput,
    DecisionOutcomeInput,
    DecisionOutcomeOutput,
    DecisionPrecedentsInput,
    DecisionPrecedentsOutput,
    DecisionRecordInput,
    DecisionRecordOutput,
)
from server.memory.app.decision import (
    handle_decision_chain,
    handle_decision_outcome,
    handle_decision_precedents,
    handle_decision_record,
)
from server.operations.base import Operation, Role

decision_record = Operation(
    name="decision.record",
    maturity="experimental",
    input_model=DecisionRecordInput,
    output_model=DecisionRecordOutput,
    handler=handle_decision_record,
    # Same category+scenario → supersedes; the record plane is versioned.
    idempotent=True,
    budget_p99_ms=1500,
    required_role=Role.WRITER,
    audit=True,
)

decision_precedents = Operation(
    name="decision.precedents",
    maturity="experimental",
    input_model=DecisionPrecedentsInput,
    output_model=DecisionPrecedentsOutput,
    handler=handle_decision_precedents,
    idempotent=True,
    budget_p99_ms=800,
    required_role=Role.VIEWER,
    audit=False,
)

decision_chain = Operation(
    name="decision.chain",
    maturity="experimental",
    input_model=DecisionChainInput,
    output_model=DecisionChainOutput,
    handler=handle_decision_chain,
    idempotent=True,
    budget_p99_ms=500,
    required_role=Role.VIEWER,
    audit=False,
)

decision_outcome = Operation(
    name="decision.outcome",
    maturity="experimental",
    input_model=DecisionOutcomeInput,
    output_model=DecisionOutcomeOutput,
    handler=handle_decision_outcome,
    idempotent=False,
    budget_p99_ms=1500,
    required_role=Role.WRITER,
    audit=True,
)

OPERATIONS: list[Operation] = [
    decision_record,
    decision_precedents,
    decision_chain,
    decision_outcome,
]
