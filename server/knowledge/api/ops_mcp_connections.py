"""Common transport declarations; enrollment inputs never enter audit subjects."""

from typing import Any

from server.knowledge.api.models_mcp_connections import (
    MCPConnectInput,
    MCPConnectOutput,
    MCPDisconnectOutput,
    MCPDiscoverOutput,
    MCPInspectInput,
    MCPInspectOutput,
    MCPListInput,
    MCPListOutput,
    MCPSlotInput,
)
from server.knowledge.app.mcp_connections import (
    handle_mcp_connect,
    handle_mcp_disconnect,
    handle_mcp_discover,
    handle_mcp_inspect,
    handle_mcp_list,
)
from server.operations.base import Operation, Role


def _connect_audit(input: MCPConnectInput, output: MCPConnectOutput) -> dict[str, Any]:
    return {"slot": output.connection.slot, "profile_id": input.profile_id}


def _disconnect_audit(input: MCPSlotInput, output: MCPDisconnectOutput) -> dict[str, Any]:
    return {"slot": input.slot, "disconnected": output.disconnected}


OPERATIONS: list[Operation] = [
    Operation(
        name="knowledge.connection.mcp_connect",
        input_model=MCPConnectInput,
        output_model=MCPConnectOutput,
        handler=handle_mcp_connect,
        # The handler reconciles its UUID; a transport replay must not bypass current policy.
        idempotent=False,
        required_role=Role.VIEWER,
        audit=True,
        writes=True,
        audit_subject=_connect_audit,
        skip_namespace_authz=True,
        maturity="experimental",
    ),
    Operation(
        name="knowledge.connection.mcp_list",
        input_model=MCPListInput,
        output_model=MCPListOutput,
        handler=handle_mcp_list,
        idempotent=True,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        maturity="experimental",
    ),
    Operation(
        name="knowledge.connection.mcp_inspect",
        input_model=MCPInspectInput,
        output_model=MCPInspectOutput,
        handler=handle_mcp_inspect,
        idempotent=True,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        maturity="experimental",
    ),
    Operation(
        name="knowledge.connection.mcp_discover",
        input_model=MCPSlotInput,
        output_model=MCPDiscoverOutput,
        handler=handle_mcp_discover,
        idempotent=True,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        maturity="experimental",
    ),
    Operation(
        name="knowledge.connection.mcp_disconnect",
        input_model=MCPSlotInput,
        output_model=MCPDisconnectOutput,
        handler=handle_mcp_disconnect,
        idempotent=False,
        required_role=Role.VIEWER,
        audit=True,
        writes=True,
        audit_subject=_disconnect_audit,
        skip_namespace_authz=True,
        maturity="experimental",
    ),
]


def _browse_operation() -> Operation:
    from server.knowledge.api.models_mcp_connections import MCPBrowseInput, MCPBrowseOutput
    from server.knowledge.app.mcp_browse import handle_mcp_browse

    return Operation(
        name="knowledge.connection.mcp_browse",
        input_model=MCPBrowseInput,
        output_model=MCPBrowseOutput,
        handler=handle_mcp_browse,
        idempotent=True,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        maturity="experimental",
    )


OPERATIONS.append(_browse_operation())
