"""The `erasure.*` operation declarations; their wire models are in `server.erasure.api.erasure`."""

from __future__ import annotations

from server.erasure.api.erasure import (
    ErasureExportInput,
    ErasureExportOutput,
    ErasureListInput,
    ErasureListOutput,
    ErasureRequestInput,
    ErasureRequestOutput,
    ErasureStatusInput,
    ErasureStatusOutput,
)
from server.erasure.app.handlers import (
    handle_erasure_export,
    handle_erasure_list,
    handle_erasure_request,
    handle_erasure_status,
)
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================


erasure_request = Operation(
    name="erasure.request",
    input_model=ErasureRequestInput,
    output_model=ErasureRequestOutput,
    handler=handle_erasure_request,
    idempotent=True,  # saga steps are individually idempotent
    # Input encodes the target via scope_type/scope_value, not a literal
    # `namespace` field, so the transport can't bind the authz grant — the
    # handler does the scope-specific check itself. WRITER (not ADMIN) lets a
    # namespace owner erase their own namespace.
    required_role=Role.WRITER,
    skip_namespace_authz=True,
    audit=True,
)

erasure_status = Operation(
    name="erasure.status",
    maturity="experimental",
    input_model=ErasureStatusInput,
    output_model=ErasureStatusOutput,
    handler=handle_erasure_status,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
    row_scoped=True,  # The handler authorizes the row's namespace
)


erasure_export = Operation(
    name="erasure.export",
    input_model=ErasureExportInput,
    output_model=ErasureExportOutput,
    handler=handle_erasure_export,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)


erasure_list = Operation(
    name="erasure.list",
    maturity="experimental",
    input_model=ErasureListInput,
    output_model=ErasureListOutput,
    handler=handle_erasure_list,
    idempotent=True,
    required_role=Role.VIEWER,
    skip_namespace_authz=True,  # handler scopes by caller role
    audit=False,
)


OPERATIONS: list[Operation] = [
    erasure_request,
    erasure_status,
    erasure_export,
    erasure_list,
]
