"""Operations over the wire models in `server.memory.api.models_proposals`."""

from __future__ import annotations

from server.memory.api.models_proposals import (
    MemoryProposalDecideInput,
    MemoryProposalDecideOutput,
    MemoryProposalsInput,
    MemoryProposalsOutput,
)
from server.memory.app.proposals_ops import (
    handle_memory_proposal_decide,
    handle_memory_proposals,
)
from server.operations.base import Operation, Role

memory_proposals = Operation(
    name="memory.proposals",
    maturity="experimental",
    input_model=MemoryProposalsInput,
    output_model=MemoryProposalsOutput,
    handler=handle_memory_proposals,
    idempotent=True,
    budget_p99_ms=400,
    required_role=Role.VIEWER,
    audit=False,
)

memory_proposal_decide = Operation(
    name="memory.proposal_decide",
    maturity="experimental",
    input_model=MemoryProposalDecideInput,
    output_model=MemoryProposalDecideOutput,
    handler=handle_memory_proposal_decide,
    idempotent=True,
    budget_p99_ms=800,
    required_role=Role.WRITER,
    audit=True,
)

OPERATIONS: list[Operation] = [memory_proposals, memory_proposal_decide]
