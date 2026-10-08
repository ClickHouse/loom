"""The `ops` operations the registry serves; wire models are in models_ops.py."""

from __future__ import annotations

from server.insights.api.models_ops import (
    OpsCapabilitiesInput,
    OpsCapabilitiesOutput,
    OpsHealthInput,
    OpsHealthOutput,
    ProjectionFailureListInput,
    ProjectionFailureListOutput,
    ProjectionFailureResolveInput,
    ProjectionFailureResolveOutput,
    ProjectionFailureRetryInput,
    ProjectionFailureRetryOutput,
)
from server.insights.app.ops import (
    handle_ops_capabilities,
    handle_ops_health,
    handle_projection_failures_list,
    handle_projection_failures_resolve,
    handle_projection_failures_retry,
)
from server.operations.base import Operation, Role

ops_capabilities = Operation(
    name="ops.capabilities",
    input_model=OpsCapabilitiesInput,
    output_model=OpsCapabilitiesOutput,
    handler=handle_ops_capabilities,
    idempotent=True,
    budget_p99_ms=100,
    required_role=Role.VIEWER,
    audit=False,
    skip_namespace_authz=True,
    deployment_scoped=True,
)


ops_health = Operation(
    name="ops.health",
    input_model=OpsHealthInput,
    output_model=OpsHealthOutput,
    handler=handle_ops_health,
    idempotent=True,
    budget_p99_ms=500,
    required_role=Role.ORG_ADMIN,
    audit=False,
    skip_namespace_authz=True,
)


projection_failures_list = Operation(
    name="ops.projection_failures.list",
    maturity="experimental",
    input_model=ProjectionFailureListInput,
    output_model=ProjectionFailureListOutput,
    handler=handle_projection_failures_list,
    idempotent=True,
    budget_p99_ms=300,
    required_role=Role.ORG_ADMIN,
    audit=False,
    skip_namespace_authz=True,
)

projection_failures_resolve = Operation(
    name="ops.projection_failures.resolve",
    maturity="experimental",
    input_model=ProjectionFailureResolveInput,
    output_model=ProjectionFailureResolveOutput,
    handler=handle_projection_failures_resolve,
    idempotent=True,
    budget_p99_ms=500,
    required_role=Role.ORG_ADMIN,
    audit=True,
    skip_namespace_authz=True,
)

projection_failures_retry = Operation(
    name="ops.projection_failures.retry",
    maturity="experimental",
    input_model=ProjectionFailureRetryInput,
    output_model=ProjectionFailureRetryOutput,
    handler=handle_projection_failures_retry,
    idempotent=True,
    budget_p99_ms=3000,
    required_role=Role.ORG_ADMIN,
    audit=True,
    skip_namespace_authz=True,
)


OPERATIONS = [
    ops_capabilities,
    ops_health,
    projection_failures_list,
    projection_failures_resolve,
    projection_failures_retry,
]
