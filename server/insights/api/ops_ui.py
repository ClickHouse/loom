"""The `ui` operations the registry serves; wire models are in models_ui.py."""

from __future__ import annotations

from server.insights.api.models_ui import (
    UiVitalsInput,
    UiVitalsOutput,
)
from server.insights.app.ui import handle_ui_vitals
from server.operations.base import Operation, Role

ui_vitals = Operation(
    name="ui.vitals",
    input_model=UiVitalsInput,
    output_model=UiVitalsOutput,
    handler=handle_ui_vitals,
    idempotent=True,
    required_role=Role.VIEWER,
    writes=True,
    maturity="experimental",
)

OPERATIONS = [ui_vitals]
