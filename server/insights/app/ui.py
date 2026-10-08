"""ui.* handlers — the dashboard's own telemetry.

`ui.vitals` records nothing itself: the transport writes the one ledger row every operation
gets, and the request's fields ride as identifier-class `args` (route, paint and long-task
times, node and call counts, build). The handler only has to exist as an operation.
"""

from __future__ import annotations

from server.insights.api.models_ui import UiVitalsInput, UiVitalsOutput


async def handle_ui_vitals(input: UiVitalsInput) -> UiVitalsOutput:
    """Record one dashboard page's web vitals; the per-op ledger row the transport writes carries
    the fields."""

    return UiVitalsOutput(recorded=True, route=input.route)
