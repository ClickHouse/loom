"""Admin-only query-history source registration; no credential is returned."""

from server.knowledge.api.models_query_history import (
    QueryHistoryAddInput,
    QueryHistoryListInput,
    QueryHistoryListOutput,
    QueryHistoryRemoveInput,
    QueryHistoryRemoveOutput,
    QueryHistorySource,
)
from server.knowledge.app.query_history_sources import (
    handle_add,
    handle_list,
    handle_remove,
    source_audit_subject,
)
from server.operations.base import Operation, Role

query_history_add = Operation(
    name="knowledge.query_history.add",
    maturity="experimental",
    input_model=QueryHistoryAddInput,
    output_model=QueryHistorySource,
    handler=handle_add,
    idempotent=False,
    required_role=Role.ADMIN,
    audit=True,
    audit_subject=source_audit_subject,
)
query_history_list = Operation(
    name="knowledge.query_history.list",
    maturity="experimental",
    input_model=QueryHistoryListInput,
    output_model=QueryHistoryListOutput,
    handler=handle_list,
    idempotent=True,
    required_role=Role.ADMIN,
)
query_history_remove = Operation(
    name="knowledge.query_history.remove",
    maturity="experimental",
    input_model=QueryHistoryRemoveInput,
    output_model=QueryHistoryRemoveOutput,
    handler=handle_remove,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
    audit_subject=source_audit_subject,
)
OPERATIONS = [query_history_add, query_history_list, query_history_remove]
