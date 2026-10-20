"""The `policy` operations the registry serves; wire models are in models_policy.py."""

from __future__ import annotations

from server.identity.api.models_policy import (
    PolicyCheckInput,
    PolicyCheckOutput,
    PolicyDecisionsInput,
    PolicyDecisionsOutput,
    PolicyDeleteInput,
    PolicyDeleteOutput,
    PolicyListInput,
    PolicyListOutput,
    PolicySetInput,
    PolicySetOutput,
)
from server.identity.app.history_policy_ops import (
    handle_policy_check,
    handle_policy_decisions,
    handle_policy_delete,
    handle_policy_list,
    handle_policy_set,
)
from server.operations.base import Operation, Role

OPERATIONS: list[Operation] = [
    Operation(
        name="policy.set",
        maturity="experimental",
        input_model=PolicySetInput,
        output_model=PolicySetOutput,
        handler=handle_policy_set,
        idempotent=True,
        required_role=Role.ADMIN,
        audit=True,
    ),
    Operation(
        name="policy.list",
        input_model=PolicyListInput,
        output_model=PolicyListOutput,
        handler=handle_policy_list,
        idempotent=True,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="policy.delete",
        maturity="experimental",
        input_model=PolicyDeleteInput,
        output_model=PolicyDeleteOutput,
        handler=handle_policy_delete,
        idempotent=True,
        required_role=Role.ADMIN,
        audit=True,
    ),
    Operation(
        name="policy.check",
        input_model=PolicyCheckInput,
        output_model=PolicyCheckOutput,
        handler=handle_policy_check,
        idempotent=True,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="policy.decisions",
        maturity="experimental",
        input_model=PolicyDecisionsInput,
        output_model=PolicyDecisionsOutput,
        handler=handle_policy_decisions,
        idempotent=True,
        required_role=Role.VIEWER,
    ),
]
