"""ClickHouse predicates for native and worker source account bindings."""

from __future__ import annotations

from server.knowledge.domain.sources import worker_connection_id

# Inventory and restore must notice a damaged worker row even if only one marker survived.
WORKER_HISTORY = "(source_kind = 'mcp' OR kind = 'mcp' OR credential_mode = 'worker')"


def source_account_match(kind: str, *, include_damaged: bool = False) -> tuple[str, str]:
    """Match exact owner binding; callers supply organization and owner predicates."""
    connection = worker_connection_id(kind)
    if connection is not None:
        marker = (
            WORKER_HISTORY
            if include_damaged
            else "source_kind = 'mcp' AND credential_mode = 'worker'"
        )
        return f"{marker} AND connection_id = {{kind:String}}", connection
    return "credential_mode = 'connected' AND source_kind = {kind:String}", kind
