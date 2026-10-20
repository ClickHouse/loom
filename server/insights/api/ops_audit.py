"""The `audit` operations the registry serves; wire models are in models_audit.py."""

from __future__ import annotations

from server.insights.api.models_audit import (
    AuditListInput,
    AuditListOutput,
)
from server.insights.app.audit import handle_audit_list
from server.operations.base import Operation, Role

audit_list = Operation(
    name="audit.list",
    maturity="experimental",
    input_model=AuditListInput,
    output_model=AuditListOutput,
    handler=handle_audit_list,
    idempotent=True,
    # VIEWER (not ADMIN) because the handler self-scopes — platform
    # admins see org-wide, regular users see only their accessible
    # namespaces. ADMIN+namespace-authz would have blocked legitimate
    # cross-namespace reads by the operator.
    required_role=Role.VIEWER,
    # Handler does its own namespace scoping; transport-level check
    # would reject the namespace=None (org-wide) case before reaching it.
    skip_namespace_authz=True,
    audit=False,  # querying audit shouldn't itself flood the audit log
)


OPERATIONS: list[Operation] = [audit_list]
