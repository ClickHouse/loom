"""Selected source setup uses the common transport and destination grants."""

from server.knowledge.api.models_mcp_sources import MCPSourcePreviewInput, MCPSourcePreviewOutput
from server.knowledge.app.mcp_source_preview import handle_mcp_source_preview
from server.operations.base import Operation, Role

OPERATIONS: list[Operation] = [
    Operation(
        name="knowledge.source.mcp_preview",
        input_model=MCPSourcePreviewInput,
        output_model=MCPSourcePreviewOutput,
        handler=handle_mcp_source_preview,
        idempotent=True,
        required_role=Role.WRITER,
        maturity="experimental",
    ),
]
