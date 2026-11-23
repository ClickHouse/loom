"""Read-only operator profile discovery on the common operation transport."""

from server.knowledge.api.models_mcp_profiles import MCPProfilesInput, MCPProfilesOutput
from server.knowledge.app.mcp_profile_api import handle_mcp_profiles
from server.operations.base import Operation, Role

OPERATIONS: list[Operation] = [
    Operation(
        name="knowledge.source.mcp_profiles",
        input_model=MCPProfilesInput,
        output_model=MCPProfilesOutput,
        handler=handle_mcp_profiles,
        idempotent=True,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        maturity="experimental",
    ),
]
