"""Operations over the wire models in `server.memory.api.models_records`."""

from __future__ import annotations

from server.memory.api.models_records import (
    MemoryRecordConflictsInput,
    MemoryRecordConflictsOutput,
    MemoryRecordHistoryInput,
    MemoryRecordHistoryOutput,
    MemoryRecordQueryInput,
    MemoryRecordQueryOutput,
    MemoryRecordWriteInput,
    MemoryRecordWriteOutput,
    MemorySchemaDecisionInput,
    MemorySchemaDecisionOutput,
    MemorySchemaListInput,
    MemorySchemaListOutput,
    MemorySchemaRegisterInput,
    MemorySchemaRegisterOutput,
)
from server.memory.app.records import (
    handle_memory_record_conflicts,
    handle_memory_record_history,
    handle_memory_record_query,
    handle_memory_record_write,
    handle_memory_schema_approve,
    handle_memory_schema_list,
    handle_memory_schema_propose,
    handle_memory_schema_register,
)
from server.operations.base import Operation, Role

memory_schema_register = Operation(
    name="memory_schema.register",
    input_model=MemorySchemaRegisterInput,
    output_model=MemorySchemaRegisterOutput,
    handler=handle_memory_schema_register,
    idempotent=False,
    budget_p99_ms=120,
    required_role=Role.ADMIN,
    audit=True,
)

memory_schema_list = Operation(
    name="memory_schema.list",
    maturity="experimental",
    input_model=MemorySchemaListInput,
    output_model=MemorySchemaListOutput,
    handler=handle_memory_schema_list,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
)

memory_schema_propose = Operation(
    name="memory_schema.propose",
    maturity="experimental",
    input_model=MemorySchemaRegisterInput,
    output_model=MemorySchemaRegisterOutput,
    handler=handle_memory_schema_propose,
    idempotent=False,
    budget_p99_ms=120,
    required_role=Role.WRITER,
    audit=True,
)

memory_schema_approve = Operation(
    name="memory_schema.approve",
    maturity="experimental",
    input_model=MemorySchemaDecisionInput,
    output_model=MemorySchemaDecisionOutput,
    handler=handle_memory_schema_approve,
    idempotent=False,
    budget_p99_ms=120,
    required_role=Role.ADMIN,
    audit=True,
)

memory_record_write = Operation(
    name="memory.record.write",
    input_model=MemoryRecordWriteInput,
    output_model=MemoryRecordWriteOutput,
    handler=handle_memory_record_write,
    idempotent=False,
    budget_p99_ms=150,
    required_role=Role.WRITER,
    audit=True,
)

memory_record_query = Operation(
    name="memory.record.query",
    input_model=MemoryRecordQueryInput,
    output_model=MemoryRecordQueryOutput,
    handler=handle_memory_record_query,
    idempotent=True,
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)

memory_record_history = Operation(
    name="memory.record.history",
    input_model=MemoryRecordHistoryInput,
    output_model=MemoryRecordHistoryOutput,
    handler=handle_memory_record_history,
    idempotent=True,
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)

memory_record_conflicts = Operation(
    name="memory.record.conflicts",
    maturity="experimental",
    input_model=MemoryRecordConflictsInput,
    output_model=MemoryRecordConflictsOutput,
    handler=handle_memory_record_conflicts,
    idempotent=True,
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)

OPERATIONS = [
    memory_schema_register,
    memory_schema_list,
    memory_schema_propose,
    memory_schema_approve,
    memory_record_write,
    memory_record_query,
    memory_record_history,
    memory_record_conflicts,
]
