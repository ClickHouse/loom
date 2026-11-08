"""Operator health and repair recommendation surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class OpsHealthInput(BaseModel):
    org: str
    namespace: str | None = None


class OpsHealthCheck(BaseModel):
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str = ""
    data: dict[str, Any] = {}
    repair: str = ""


class OpsHealthOutput(BaseModel):
    # One entry per health check (schema contracts, embedding stubs, parts, op mix, workers,
    # ...), each with its status and, when not `ok`, a suggested repair.
    checks: list[OpsHealthCheck]
    # Per background worker in this process: last run time, duration, outcome, the last
    # error's kind (`last_error_kind`), consecutive failures and total runs; empty before
    # any worker has run. The error's text is in `/v1/admin/workers.status`.
    worker_status: dict[str, dict[str, Any]] = {}
    # `fail` when any check failed, else `warn` when any warned, else `ok`.
    overall: Literal["ok", "warn", "fail"]


class ProjectionFailureListInput(BaseModel):
    org: str
    namespace: str | None = None
    # Only failures of this projection (the derived table, such as `memory_chunks` or
    # `entity_edges`); empty lists every projection.
    projection: str = ""
    # `active` lists open failures, `resolved` repaired ones, `abandoned` those the
    # repair gave up on (the reason is in `error_message`), empty all of them.
    status: Literal["active", "resolved", "abandoned", ""] = "active"
    # At most this many failures, newest first.
    limit: int = 100


class ProjectionFailureRow(BaseModel):
    failure_id: str
    org: str
    namespace: str
    agent: str = ""
    projection: str
    source_operation: str = ""
    source_resource_id: str = ""
    memory_keys: list[str] = Field(default_factory=list)
    # The exception's kind, or `-` for a reason Loom recorded as text.
    error_type: str = ""
    # A Loom-written reason whole; for an exception, `abandoned: <reason>` once a retry
    # gave up on the row, `""` otherwise (its text stays in the table for an operator).
    error_message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    created_at: str | None = None
    resolved_at: str | None = None


class ProjectionFailureListOutput(BaseModel):
    # The recorded failures matching the filters, newest first.
    rows: list[ProjectionFailureRow]
    # Number of rows returned (not the count beyond `limit`).
    total: int


class ProjectionFailureResolveInput(BaseModel):
    org: str
    # The failures to mark resolved without retrying them, 1 to 500 ids.
    failure_ids: list[str] = Field(..., min_length=1, max_length=500)


class ProjectionFailureResolveOutput(BaseModel):
    # Non-empty ids submitted for resolution; ids that match no active failure are counted.
    resolved: int


class ProjectionFailureRetryInput(BaseModel):
    org: str
    namespace: str | None = None
    # Only retry failures of this projection; empty retries every projection.
    projection: str = ""
    # At most this many active failures are retried, newest first.
    limit: int = 50


class ProjectionFailureRetryRow(BaseModel):
    failure_id: str
    projection: str
    retried: bool
    resolved: bool
    # Given up on: no retry can do the work, `reason` says why; it left the active queue.
    abandoned: bool = False
    reason: str = ""
    error: str = ""


class ProjectionFailureRetryOutput(BaseModel):
    # One entry per failure attempted, with whether it was retried and resolved and why not.
    results: list[ProjectionFailureRetryRow] = Field(default_factory=list)
    # Failures whose projection has a retry path and was attempted.
    retried: int
    # Failures whose retry succeeded and which are now marked resolved.
    resolved: int
    # Failures the retry gave up on, now marked abandoned.
    abandoned: int = 0


# ============================================================
# ops.capabilities — the deployment-level catalogue
# ============================================================


class OpsCapabilitiesInput(BaseModel):
    """Identity is optional: a client discovers the surface with whatever
    token it holds (role check only; no namespace is addressed)."""

    org: str = ""
    consumer: str = Field(default="", max_length=64)


class OpCapability(BaseModel):
    name: str
    path: str
    mcp_tool: str
    mcp_tier: Literal["core", "code", "extended", "hidden"]
    required_role: str
    budget_p99_ms: int
    idempotent: bool
    audit: bool
    request_fields: list[str] = Field(default_factory=list)
    response_fields: list[str] = Field(default_factory=list)
    # Additive: every MCP toolset whose listing carries the op; empty for a hidden op
    # off the assistant roster.
    toolsets: list[str] = Field(default_factory=list)


class OpsCapabilitiesOutput(BaseModel):
    """Every served operation with its required role, latency budget,
    request fields and MCP tier, and the build that serves them. A client
    reads `request_fields[op]` once instead of probing a route with a 404."""

    # The git commit the serving image was built from; empty in a source run.
    build_commit: str = ""
    # The wire schema version, as the `X-Schema-Version` header names it.
    schema_version: str = ""
    # Every registered operation, sorted by name, hidden ones included.
    ops: list[OpCapability] = Field(default_factory=list)
    # Every field of every request model that names the consumer — the
    # tag stored on every ledger row.
    consumer_field: str = "consumer"
