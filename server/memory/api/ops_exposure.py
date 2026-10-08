"""Operations over the wire models in `server.memory.api.models_exposure`."""

from __future__ import annotations

from server.memory.api.models_exposure import (
    ExposureSummaryInput,
    ExposureSummaryOutput,
    MemoryOwnerReportInput,
    MemoryOwnerReportOutput,
)
from server.memory.app.exposure import (
    handle_memory_exposure_summary,
    handle_memory_owner_report,
)
from server.operations.base import Operation, Role

memory_exposure_summary = Operation(
    name="memory.exposure_summary",
    maturity="experimental",
    input_model=ExposureSummaryInput,
    output_model=ExposureSummaryOutput,
    handler=handle_memory_exposure_summary,
    idempotent=True,
    budget_p99_ms=300,
    # Who-read-what is an operator view of other principals' activity.
    required_role=Role.ADMIN,
    audit=False,
)


memory_owner_report = Operation(
    name="memory.owner_report",
    maturity="experimental",
    input_model=MemoryOwnerReportInput,
    output_model=MemoryOwnerReportOutput,
    handler=handle_memory_owner_report,
    idempotent=True,
    budget_p99_ms=800,
    # The owner's own rows: a viewer may ask about themself; the read-scope
    # binding, not the role, is what keeps it to themself.
    required_role=Role.VIEWER,
    audit=False,
)
