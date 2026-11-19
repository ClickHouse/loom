"""Opt-in query-history sources that queue definitions for an admin to review."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.operations.read_projection import ProjectedRow


class QueryHistoryAddInput(BaseModel):
    """Register a read-only ClickHouse query-history source for definition drafts."""

    org: str
    namespace: str
    # Registration name within this namespace.
    name: str = Field(min_length=1, max_length=100)
    # ClickHouse host; follows the tracker's host-vetting policy.
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(
        default=8443,
        ge=1,
        le=65535,
        description="The ClickHouse HTTP interface port; default 8443.",
    )
    secure: bool = Field(
        default=True, description="Whether the HTTP connection uses TLS; default true."
    )
    # Read-only ClickHouse account with SELECT on system.query_log.
    user: str = Field(min_length=1, max_length=255)
    # Sealed at rest and never returned.
    password: str = Field(max_length=4096)
    # Only queries whose databases all belong to this allowlist are considered.
    databases: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        min_length=1, max_length=100
    )
    # Optional configured ClickHouse cluster, read through clusterAllReplicas.
    cluster: str = Field(default="", max_length=255)


class QueryHistorySource(ProjectedRow):
    name: str
    source_id: str
    kind: Literal["clickhouse_queries"] = "clickhouse_queries"
    host: str
    port: int
    secure: bool
    user: str
    databases: list[str]
    cluster: str = ""
    created_at: datetime


class QueryHistoryListInput(BaseModel):
    """List the namespace's registered query-history sources."""

    fields: list[str] | None = None
    org: str
    namespace: str
    limit: int = Field(
        default=50, ge=1, le=1000, description="Registered sources to return, at most; default 50."
    )


class QueryHistoryListOutput(BaseModel):
    # Whether the operator permits new registrations and query-history turns.
    enabled: bool = False
    sources: list[QueryHistorySource] = Field(default_factory=list)


class QueryHistoryRemoveInput(BaseModel):
    """Remove a query-history source registration from the namespace."""

    org: str
    namespace: str
    name: str = Field(description="The registration name of the query-history source to remove.")


class QueryHistoryRemoveOutput(BaseModel):
    removed: bool
