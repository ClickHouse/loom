"""The `llm` operation the registry serves; wire models are in models_llm.py."""

from __future__ import annotations

from server.insights.api.models_llm import LlmCompleteInput, LlmCompleteOutput
from server.insights.app.llm_serve import handle_llm_complete
from server.operations.base import Operation, Role

llm_complete = Operation(
    name="llm.complete",
    maturity="experimental",
    input_model=LlmCompleteInput,
    output_model=LlmCompleteOutput,
    handler=handle_llm_complete,
    # A completion at temperature > 0 differs per call; no retry layer may replay it.
    idempotent=False,
    budget_p99_ms=120_000,
    required_role=Role.VIEWER,
    audit=False,
    skip_namespace_authz=True,
)

OPERATIONS = [llm_complete]
