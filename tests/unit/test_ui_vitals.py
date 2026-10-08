"""The dashboard reports its own paint.

`ui.vitals` is a dashboard-only operation the /me shell posts once per page
view; the ledger row the transport writes is the record, and the view's
fields ride as identifier-class args. Hidden from the MCP listing.
"""

from __future__ import annotations

import pytest

from server.insights.api.models_ui import UiVitalsInput
from server.insights.app.ui import handle_ui_vitals
from server.observability.event_args import project_args
from server.operations.registry import OPERATIONS_BY_NAME
from server.operations.toolsets import _MCP_HIDDEN_OPS


def test_the_operation_is_registered_experimental_and_hidden() -> None:
    op = OPERATIONS_BY_NAME["ui.vitals"]
    assert op.maturity == "experimental" and op.idempotent is True
    assert "ui.vitals" in _MCP_HIDDEN_OPS


@pytest.mark.asyncio
async def test_the_handler_records_nothing_but_answers() -> None:
    out = await handle_ui_vitals(
        UiVitalsInput(org="o", namespace="n", route="memories", lcp_ms=796, long_task_ms=238)
    )
    assert out.recorded is True and out.route == "memories"


def test_the_fields_ride_the_ledger_row_as_identifiers() -> None:
    body = UiVitalsInput(
        org="o",
        namespace="n",
        route="admin/usage",
        lcp_ms=476,
        long_task_ms=110,
        inp_ms=88,
        dom_nodes=526,
        api_calls=7,
        api_ms=280,
        build="31bc74135",
        cold=False,
    ).model_dump()
    args = project_args(body)
    assert args["route"] == "admin/usage" and args["lcp_ms"] == "476"
    assert args["long_task_ms"] == "110" and args["dom_nodes"] == "526"
    assert args["inp_ms"] == "88"
    assert args["api_calls"] == "7" and args["api_ms"] == "280"
    assert args["build"] == "31bc74135" and args["cold"] == "false"
    assert "org" not in args and "namespace" not in args  # identity columns, not args


def test_a_navigation_carries_no_lcp() -> None:
    args = project_args(
        UiVitalsInput(org="o", namespace="n", route="runs", cold=False).model_dump()
    )
    assert "lcp_ms" not in args  # None is not an identifier


def test_a_long_lived_view_span_is_accepted() -> None:
    """A view left open for an hour of polls (api_ms in the millions) is
    accepted — a day is the ceiling."""
    from server.insights.api.models_ui import UiVitalsInput

    row = UiVitalsInput(
        org="o", namespace="n", route="tasks", api_calls=95, api_ms=4_099_009, inp_ms=48
    )
    assert row.api_ms == 4_099_009
