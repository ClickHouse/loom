"""The `admin.users` operations the registry serves; wire models are in models_admin_users.py."""

from __future__ import annotations

from server.identity.api.models_admin_users import (
    AdminUsersInput,
    AdminUsersOutput,
)
from server.identity.app.admin_users import handle_admin_users
from server.operations.base import Operation, Role

admin_users = Operation(
    name="admin.users",
    maturity="experimental",
    input_model=AdminUsersInput,
    output_model=AdminUsersOutput,
    handler=handle_admin_users,
    idempotent=True,
    budget_p99_ms=300,
    required_role=Role.ORG_ADMIN,
    audit=False,
    # Caller is platform/org admin — namespace-level grants don't apply.
    skip_namespace_authz=True,
)


OPERATIONS: list[Operation] = [admin_users]
