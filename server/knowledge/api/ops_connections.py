"""Personal connected-account operations; their handlers derive ownership from auth."""

from server.knowledge.api.models_connections import (
    ConnectionDisconnectInput,
    ConnectionDisconnectOutput,
    ConnectionListInput,
    ConnectionListOutput,
)
from server.knowledge.app.connections import handle_connection_disconnect, handle_connection_list
from server.operations.base import Operation, Role

OPERATIONS: list[Operation] = [
    Operation(
        name="connection.list",
        input_model=ConnectionListInput,
        output_model=ConnectionListOutput,
        handler=handle_connection_list,
        idempotent=True,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        maturity="experimental",
    ),
    Operation(
        name="connection.disconnect",
        input_model=ConnectionDisconnectInput,
        output_model=ConnectionDisconnectOutput,
        handler=handle_connection_disconnect,
        idempotent=True,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        writes=True,
        maturity="experimental",
    ),
]
