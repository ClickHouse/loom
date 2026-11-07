"""Operations over the wire models in `server.memory.api.models_loom_search`."""

from __future__ import annotations

from server.memory.api.models_loom_search import SearchInput, SearchOutput
from server.memory.app.loom_search.handler import handle_search
from server.memory.app.served_reads import records_served, search_query_keys
from server.operations.base import Operation, Role

search = Operation(
    name="search.query",
    # MCP serves it as the `loom_search` tool (synthetic descriptor).
    mcp_tool="loom_search",
    input_model=SearchInput,
    output_model=SearchOutput,
    handler=records_served(search_query_keys)(handle_search),
    idempotent=True,
    required_role=Role.VIEWER,
    maturity="experimental",
)

OPERATIONS = [search]
