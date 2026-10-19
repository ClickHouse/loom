"""The `grants` operations the registry serves; wire models are in models_grants.py."""

from __future__ import annotations

from server.identity.api.models_grants import (
    GrantsApproveInput,
    GrantsApproveOutput,
    GrantsCreateInput,
    GrantsCreateOutput,
    GrantsListInput,
    GrantsListOutput,
    GrantsPendingForMeInput,
    GrantsPendingForMeOutput,
    GrantsRevokeInput,
    GrantsRevokeOutput,
)
from server.identity.app.grants import (
    handle_grants_approve,
    handle_grants_create,
    handle_grants_list,
    handle_grants_pending_for_me,
    handle_grants_revoke,
)
from server.operations.base import Operation, Role

grants_create = Operation(
    name="grants.create",
    input_model=GrantsCreateInput,
    output_model=GrantsCreateOutput,
    handler=handle_grants_create,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)

grants_list = Operation(
    name="grants.list",
    input_model=GrantsListInput,
    output_model=GrantsListOutput,
    handler=handle_grants_list,
    idempotent=True,
    # Any member of the namespace can see who else is in. Owners can
    # still revoke; knowing the member list doesn't leak content.
    # Non-members fall through check_authorized's grants-table lookup
    # and get 403.
    required_role=Role.VIEWER,
    audit=False,
)

grants_pending_for_me = Operation(
    name="grants.pending_for_me",
    maturity="experimental",
    input_model=GrantsPendingForMeInput,
    output_model=GrantsPendingForMeOutput,
    handler=handle_grants_pending_for_me,
    idempotent=True,
    # Org-level: the caller's own admin grants decide which namespaces'
    # queues are read; the transport's role gate is the
    # floor, the query is the scope.
    required_role=Role.VIEWER,
    audit=False,
    deployment_scoped=True,
)

grants_revoke = Operation(
    name="grants.revoke",
    maturity="experimental",
    input_model=GrantsRevokeInput,
    output_model=GrantsRevokeOutput,
    handler=handle_grants_revoke,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)

grants_approve = Operation(
    name="grants.approve",
    maturity="experimental",
    input_model=GrantsApproveInput,
    output_model=GrantsApproveOutput,
    handler=handle_grants_approve,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)


OPERATIONS: list[Operation] = [
    grants_create,
    grants_list,
    grants_pending_for_me,
    grants_revoke,
    grants_approve,
]
