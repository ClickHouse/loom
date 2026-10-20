"""The `org` model-call operations the registry serves; wire models are in
models_model_calls.py."""

from __future__ import annotations

from server.identity.api.models_model_calls import (
    OrgModelCallsInput,
    OrgModelCallsOutput,
    OrgSetModelCallsInput,
)
from server.identity.app.model_calls import handle_org_model_calls, handle_org_set_model_calls
from server.operations.base import Operation, Role

# Org-wide: the org admin of the org named, never a namespace's admin.
org_set_model_calls = Operation(
    name="org.set_model_calls",
    maturity="experimental",
    input_model=OrgSetModelCallsInput,
    output_model=OrgModelCallsOutput,
    handler=handle_org_set_model_calls,
    idempotent=True,
    required_role=Role.ORG_ADMIN,
    audit=True,
    skip_namespace_authz=True,
)

org_model_calls = Operation(
    name="org.model_calls",
    maturity="experimental",
    input_model=OrgModelCallsInput,
    output_model=OrgModelCallsOutput,
    handler=handle_org_model_calls,
    idempotent=True,
    required_role=Role.ORG_ADMIN,
    audit=False,
    skip_namespace_authz=True,
)


OPERATIONS: list[Operation] = [org_set_model_calls, org_model_calls]
