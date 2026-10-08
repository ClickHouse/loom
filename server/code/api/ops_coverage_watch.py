"""The `code.coverage.watch` / `code.coverage.unwatch` operations."""

from __future__ import annotations

from server.code.api.models_coverage_watch import (
    CodeCoverageUnwatchInput,
    CodeCoverageUnwatchOutput,
    CodeCoverageWatchInput,
    CodeCoverageWatchOutput,
)
from server.code.app.coverage_watch import (
    handle_code_coverage_unwatch,
    handle_code_coverage_watch,
)
from server.operations.base import Operation, Role

OPERATIONS: list[Operation] = [
    Operation(
        name="code.coverage.watch",
        input_model=CodeCoverageWatchInput,
        output_model=CodeCoverageWatchOutput,
        handler=handle_code_coverage_watch,
        idempotent=True,
        # an inbox read, a state read, a few git calls and up to three writes
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
        writes=True,
    ),
    Operation(
        name="code.coverage.unwatch",
        input_model=CodeCoverageUnwatchInput,
        output_model=CodeCoverageUnwatchOutput,
        handler=handle_code_coverage_unwatch,
        idempotent=True,
        budget_p99_ms=500,
        required_role=Role.VIEWER,
        writes=True,
    ),
]
