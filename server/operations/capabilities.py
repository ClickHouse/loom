"""The deployment-level operation catalogue served by `ops.capabilities`.

`capability_snapshot()` is the one place the catalogue is built from the
registry, so the served op, `scripts/codegen/wire_changelog.py` (which diffs two
revisions' snapshots) and the tests cannot drift.
"""

from __future__ import annotations

from typing import Any

# The wire schema version every answer names: the `X-Schema-Version` header
# and `ops.capabilities.schema_version`. Raised by hand with a wire change
# that breaks clients.
SCHEMA_VERSION = "1"


def _mcp_tier(op_name: str) -> str:
    """Where the op sits on the MCP surface: 'core' (listed on the default
    toolset), 'code' (listed when LOOM_MCP_CODE_TOOLS is on), 'hidden'
    (never listed; callable by name where the transport allows) or
    'extended' (everything else: listed on `all` when `listed_under_all`,
    otherwise callable by name)."""
    from server.operations import toolsets

    if op_name in toolsets._MCP_HIDDEN_OPS:
        return "hidden"
    if op_name in toolsets._MCP_CORE_OPS or op_name in toolsets.SYNTHETIC_TOOL_OPS:
        return "core"
    if op_name in toolsets._MCP_CODE_CORE_OPS:
        return "code"
    return "extended"


def capability_snapshot() -> dict[str, Any]:
    """The deployment-level catalogue: every served operation with its
    required role, latency budget, request fields and MCP tier. Built from
    the registry; the same dict the changelog script reads at a revision."""
    from server.operations.registry import ALL_OPERATIONS
    from server.operations.toolsets import op_toolsets
    from server.settings import settings

    ops: dict[str, dict[str, Any]] = {}
    for op in ALL_OPERATIONS:
        ops[op.name] = {
            "path": op.http_path,
            "mcp_tool": op.mcp_tool_name,
            "mcp_tier": _mcp_tier(op.name),
            "required_role": op.required_role.name,
            "budget_p99_ms": int(op.budget_p99_ms),
            # The request shapes the handler prices separately
            "shape_budgets_p99_ms": {k: int(v) for k, v in op.shape_budgets_p99_ms.items()},
            "idempotent": bool(op.idempotent),
            "audit": bool(op.audit),
            "request_fields": sorted(op.input_model.model_fields),
            "response_fields": sorted(op.output_model.model_fields),
            "toolsets": op_toolsets(op.name),
        }
    return {
        "build_commit": str(settings.build_commit),
        "schema_version": SCHEMA_VERSION,
        "ops": dict(sorted(ops.items())),
    }


# The `/v1/operations` row: a projection of the catalogue entry, plus the name.
OPERATION_ROW_FIELDS: tuple[str, ...] = (
    "path",
    "mcp_tool",
    "idempotent",
    "budget_p99_ms",
    "shape_budgets_p99_ms",
    "required_role",
)


def operation_rows() -> list[dict[str, Any]]:
    """The `/v1/operations` discovery list: each op's catalogue entry cut to
    `OPERATION_ROW_FIELDS`, in registry order."""
    from server.operations.registry import ALL_OPERATIONS

    ops = capability_snapshot()["ops"]
    return [
        {"name": op.name, **{k: ops[op.name][k] for k in OPERATION_ROW_FIELDS}}
        for op in ALL_OPERATIONS
        if op.available  # a feature that is off lists none of its ops
    ]


__all__ = ["OPERATION_ROW_FIELDS", "SCHEMA_VERSION", "capability_snapshot", "operation_rows"]
