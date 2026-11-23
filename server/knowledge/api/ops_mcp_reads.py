"""Direct live reads require the selected source's current owner authority."""

from server.knowledge.api.models_mcp_reads import MCPReadInput, MCPReadOutput
from server.knowledge.app.mcp_browse import handle_mcp_read
from server.operations.base import Operation, Role

OPERATIONS: list[Operation] = [
    Operation(
        name="knowledge.source.mcp_read",
        input_model=MCPReadInput,
        output_model=MCPReadOutput,
        handler=handle_mcp_read,
        idempotent=True,
        required_role=Role.VIEWER,
        maturity="experimental",
    ),
]
