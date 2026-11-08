"""The analysis operations the registry serves; wire models are in models_analysis.py."""

from __future__ import annotations

from server.insights.api.models_analysis import (
    InsightsCatalogInput,
    InsightsCatalogOutput,
    InsightsQueryInput,
    InsightsQueryOutput,
)
from server.insights.app.analysis import handle_insights_catalog, handle_insights_query
from server.operations.base import Operation, Role

insights_query = Operation(
    name="insights.query",
    maturity="experimental",
    input_model=InsightsQueryInput,
    output_model=InsightsQueryOutput,
    handler=handle_insights_query,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

insights_catalog = Operation(
    name="insights.catalog",
    maturity="experimental",
    input_model=InsightsCatalogInput,
    output_model=InsightsCatalogOutput,
    handler=handle_insights_catalog,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

OPERATIONS: list[Operation] = [insights_query, insights_catalog]
