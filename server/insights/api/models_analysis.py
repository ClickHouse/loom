"""Wire models of `insights.query` and `insights.catalog`: read-only SQL over
the analysis allowlist, held to the token's org and namespace by ClickHouse."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InsightsQueryInput(BaseModel):
    org: str
    namespace: str
    sql: str = Field(
        ...,
        min_length=1,
        max_length=20_000,
        description=(
            "One SELECT or WITH statement over the tables insights.catalog lists; it reads "
            "only this namespace's rows. A trailing FORMAT is a syntax error, and SETTINGS "
            "cannot raise a cap or change the scope."
        ),
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description=(
            "Rows to return at most (default 100); the deployment's cap "
            "(LOOM_CH_ANALYSIS_MAX_RESULT_ROWS) wins when lower."
        ),
    )


class AnalysisColumn(BaseModel):
    name: str
    type: str


class InsightsQueryOutput(BaseModel):
    # The statement's result columns, each with its ClickHouse type.
    columns: list[AnalysisColumn] = Field(default_factory=list)
    # One list per row, in `columns` order; dates, UUIDs and decimals as strings.
    rows: list[list[Any]] = Field(default_factory=list)
    # How many rows `rows` holds, at most `limit`.
    row_count: int = 0
    # More rows matched than `limit`; narrow the statement or aggregate.
    truncated: bool = False
    # The digest the request ledger records for `sql` (`sha256:<hex>/<length>`),
    # which a citation of the statement is checked against.
    sql_digest: str = ""


class InsightsCatalogInput(BaseModel):
    org: str
    namespace: str


class CatalogColumn(BaseModel):
    name: str
    type: str
    comment: str


class CatalogTable(BaseModel):
    name: str
    comment: str
    # How to read it correctly: one live row per key or one row per event, a sum to take.
    notes: str
    columns: list[CatalogColumn]


class InsightsCatalogOutput(BaseModel):
    # The tables insights.query reads, with their columns and how to read them.
    tables: list[CatalogTable]
    # The most rows one statement returns; past it the answer is truncated.
    max_result_rows: int
    # The longest one statement runs before ClickHouse stops it.
    max_execution_s: float
    # The most rows one statement may read; add a time or key filter past it.
    max_rows_to_read: int
