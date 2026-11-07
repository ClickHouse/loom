"""Operations over the wire models in `server.memory.api.models_procedure`."""

from __future__ import annotations

from server.memory.api.models_procedure import (
    ProcedureAddInput,
    ProcedureAddOutput,
    ProcedureEvolveInput,
    ProcedureEvolveOutput,
    ProcedureForkInput,
    ProcedureForkOutput,
    ProcedureListInput,
    ProcedureListOutput,
    ProcedureLocateInput,
    ProcedureLocateOutput,
    ProcedureMatchInput,
    ProcedureMatchOutput,
    ProcedureRecordRunInput,
    ProcedureRecordRunOutput,
    ProcedureReviseInput,
    ProcedureReviseOutput,
)
from server.memory.app.procedure import (
    handle_procedure_add,
    handle_procedure_list,
    handle_procedure_locate,
    handle_procedure_match,
    handle_procedure_record_run,
)
from server.memory.app.procedure_evolve import (
    handle_procedure_evolve,
    handle_procedure_fork,
    handle_procedure_revise,
)
from server.memory.app.served_reads import procedure_keys, records_served
from server.operations.base import Operation, Role

procedure_add = Operation(
    name="procedure.add",
    input_model=ProcedureAddInput,
    output_model=ProcedureAddOutput,
    handler=handle_procedure_add,
    idempotent=False,
    required_role=Role.WRITER,
    audit=False,
)

procedure_match = Operation(
    name="procedure.match",
    input_model=ProcedureMatchInput,
    output_model=ProcedureMatchOutput,
    handler=records_served(procedure_keys)(handle_procedure_match),
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

procedure_list = Operation(
    name="procedure.list",
    maturity="experimental",
    input_model=ProcedureListInput,
    output_model=ProcedureListOutput,
    handler=handle_procedure_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)


procedure_record_run = Operation(
    name="procedure.record_run",
    maturity="experimental",
    input_model=ProcedureRecordRunInput,
    output_model=ProcedureRecordRunOutput,
    handler=handle_procedure_record_run,
    idempotent=False,
    required_role=Role.WRITER,
    audit=False,
)

procedure_evolve = Operation(
    name="procedure.evolve",
    maturity="experimental",
    input_model=ProcedureEvolveInput,
    output_model=ProcedureEvolveOutput,
    handler=handle_procedure_evolve,
    idempotent=False,
    required_role=Role.WRITER,
    audit=False,
)


procedure_locate = Operation(
    name="procedure.locate",
    maturity="experimental",
    input_model=ProcedureLocateInput,
    output_model=ProcedureLocateOutput,
    handler=handle_procedure_locate,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)


procedure_revise = Operation(
    name="procedure.revise",
    maturity="experimental",
    input_model=ProcedureReviseInput,
    output_model=ProcedureReviseOutput,
    handler=handle_procedure_revise,
    idempotent=False,
    required_role=Role.WRITER,
    audit=True,
)


procedure_fork = Operation(
    name="procedure.fork",
    maturity="experimental",
    input_model=ProcedureForkInput,
    output_model=ProcedureForkOutput,
    handler=handle_procedure_fork,
    idempotent=False,
    required_role=Role.WRITER,
    audit=True,
)


OPERATIONS: list[Operation] = [
    procedure_add,
    procedure_match,
    procedure_list,
    procedure_record_run,
    procedure_evolve,
    procedure_locate,
    procedure_revise,
    procedure_fork,
]
