"""Operations over the wire models in `server.memory.api.models_facts`."""

from __future__ import annotations

from server.memory.api.models_facts import (
    FactsExpireByAdapterInput,
    FactsExpireByAdapterOutput,
    FactsListInput,
    FactsListOutput,
    FactsPathInput,
    FactsPathOutput,
    FactsRelatedInput,
    FactsRelatedOutput,
    FactsTraverseInput,
    FactsTraverseOutput,
)
from server.memory.app.facts import (
    handle_facts_expire_by_adapter,
    handle_facts_list,
    handle_facts_path,
    handle_facts_related,
    handle_facts_traverse,
)
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================


facts_list = Operation(
    name="facts.list",
    input_model=FactsListInput,
    output_model=FactsListOutput,
    handler=handle_facts_list,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=False,
)

facts_related = Operation(
    name="facts.related",
    maturity="experimental",
    input_model=FactsRelatedInput,
    output_model=FactsRelatedOutput,
    handler=handle_facts_related,
    idempotent=True,
    budget_p99_ms=60,
    required_role=Role.VIEWER,
    audit=False,
)

facts_traverse = Operation(
    name="facts.traverse",
    input_model=FactsTraverseInput,
    output_model=FactsTraverseOutput,
    handler=handle_facts_traverse,
    idempotent=True,
    # Recursive CTE — heavier than a single hop.
    budget_p99_ms=200,
    required_role=Role.VIEWER,
    audit=False,
)

facts_path = Operation(
    name="facts.path",
    maturity="experimental",
    input_model=FactsPathInput,
    output_model=FactsPathOutput,
    handler=handle_facts_path,
    idempotent=True,
    # One frontier query per hop, at most max_hops of them.
    budget_p99_ms=250,
    required_role=Role.VIEWER,
    audit=False,
)

facts_expire_by_adapter = Operation(
    name="facts.expire_by_adapter",
    maturity="experimental",
    input_model=FactsExpireByAdapterInput,
    output_model=FactsExpireByAdapterOutput,
    handler=handle_facts_expire_by_adapter,
    idempotent=False,
    budget_p99_ms=500,
    required_role=Role.ADMIN,
    audit=True,
)


OPERATIONS: list[Operation] = [
    facts_list,
    facts_related,
    facts_traverse,
    facts_path,
    facts_expire_by_adapter,
]
