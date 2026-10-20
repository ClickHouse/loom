"""The `run.*` operation declarations; their wire models are in `models_run`."""

from __future__ import annotations

from server.operations.base import Operation, Role
from server.tasks.api.models_run import (
    RunBeginInput,
    RunBeginOutput,
    RunCompleteInput,
    RunCompleteOutput,
    RunGetInput,
    RunGetOutput,
    RunGradeInput,
    RunGradeOutput,
    RunListInput,
    RunListOutput,
)
from server.tasks.app.run import (
    handle_run_begin,
    handle_run_complete,
    handle_run_get,
    handle_run_grade,
    handle_run_list,
)

run_begin = Operation(
    name="run.begin",
    input_model=RunBeginInput,
    output_model=RunBeginOutput,
    handler=handle_run_begin,
    # Re-sending the same run_id replaces the row (RMT), never duplicates.
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

run_complete = Operation(
    name="run.complete",
    input_model=RunCompleteInput,
    output_model=RunCompleteOutput,
    handler=handle_run_complete,
    # Re-completing appends usage events again; callers complete once.
    idempotent=False,
    required_role=Role.WRITER,
    audit=True,
)

run_get = Operation(
    name="run.get",
    input_model=RunGetInput,
    output_model=RunGetOutput,
    handler=handle_run_get,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

run_list = Operation(
    name="run.list",
    input_model=RunListInput,
    output_model=RunListOutput,
    handler=handle_run_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

run_grade = Operation(
    name="run.grade",
    maturity="experimental",
    input_model=RunGradeInput,
    output_model=RunGradeOutput,
    handler=handle_run_grade,
    # A regrade with the same verdict and source changes nothing.
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

OPERATIONS: list[Operation] = [run_begin, run_complete, run_get, run_list, run_grade]
