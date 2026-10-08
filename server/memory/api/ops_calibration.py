"""Operations over the wire models in `server.memory.api.models_calibration`."""

from __future__ import annotations

from server.memory.api.models_calibration import CalibrationGetInput, CalibrationGetOutput
from server.memory.app.calibration import handle_calibration_get
from server.operations.base import Operation, Role

calibration_get = Operation(
    name="calibration.get",
    maturity="experimental",
    input_model=CalibrationGetInput,
    output_model=CalibrationGetOutput,
    handler=handle_calibration_get,
    idempotent=True,
    budget_p99_ms=60,
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS: list[Operation] = [calibration_get]
