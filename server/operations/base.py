"""Operation dataclass — the load-bearing pattern of the Control Plane.

HTTP/JSON and MCP transports both derive from the same Operation declaration.
Adding an operation = creating one declaration; both transports auto-generate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Literal, get_args

from pydantic import BaseModel
from pydantic.config import JsonDict


class Role(IntEnum):
    """Roles in ascending privilege order. Comparison is meaningful."""

    VIEWER = 10
    WRITER = 20
    ADMIN = 30
    ORG_ADMIN = 40
    PLATFORM_ADMIN = 50


# The roles a namespace grant (and a scoped token's `ns_roles` entry) can
# hold, lowest first. ORG_ADMIN and PLATFORM_ADMIN are never per namespace.
GrantRoleStr = Literal["viewer", "writer", "admin"]
GRANT_ROLES: tuple[GrantRoleStr, ...] = get_args(GrantRoleStr)
_GRANT_RANK: dict[str, int] = {r: i for i, r in enumerate(GRANT_ROLES, start=1)}


def grant_rank(role: str | None) -> int:
    """A grant role name's rank (viewer 1, writer 2, admin 3); 0 for any other
    value, so an unknown or blank role never outranks a real one."""
    return _GRANT_RANK.get(role or "", 0)


# A handler takes its own operation's input model, which the transports
# validate before the call. Callable parameters are contravariant, so no
# single model type can name "the op's input" across a mixed registry.
HandlerFn = Callable[[Any], Awaitable[BaseModel]]

# Maturity of an operation on the wire. ``stable`` has a
# production caller in the 30-day reach census; ``experimental`` has a reach
# path but no call in 30 days on the deployment the census read. A census run
# flips the label, never a silent edit.
Maturity = Literal["stable", "experimental"]
MATURITIES: tuple[Maturity, ...] = get_args(Maturity)


# An input field an agent never sets — a ranking or bench
# knob, a pipeline switch, provenance a transport stamps — carries this
# marker. The MCP tools/list schema omits it (every listed property is paid
# for on every turn of every session); the op still ACCEPTS it, over MCP by
# name and over HTTP / the SDKs, so a hidden field is unlisted, not removed.
MCP_HIDDEN: JsonDict = {"x-mcp-hidden": True}


@dataclass(frozen=True)
class Operation:
    """One declared operation on the Loom wire surface.

    Attributes:
        name: dotted operation name (e.g. ``memory.get``). The HTTP path
            is ``/v1/{name}``; the MCP tool name is ``loom_{name.replace('.', '_')}``.
        input_model: Pydantic input model.
        output_model: Pydantic output model.
        handler: async callable: input model → output model.
        idempotent: if true, safe to retry on transient failure.
        budget_p99_ms: design-target latency budget. Exceeded by a successful
            handler emits a `budget.violated` event on loom.events (LOOM_LATENCY_BUDGET_MULTIPLIER
            scales the threshold for noisy / WAN deployments).
        required_role: minimum role required to call this operation.
        shape_budgets_p99_ms: p99 budgets for request shapes the handler
            declares at run time: ``{shape: ms}``. A handler
            that notes a shape (``server.observability.budget_shape``) is held
            to that shape's budget; the catalogue (``/v1/operations``) lists
            them so a client can size its own timeouts per shape.
        audit: if true, success writes one row to audit_log.
        maturity: ``stable`` | ``experimental`` (MATURITIES).
            Declared per op; the reach census (scripts/codegen/op_reach_census.py,
            tests/unit/test_op_reach_census.py) fails an op that has no
            reach path and is not experimental.
    """

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: HandlerFn
    idempotent: bool
    budget_p99_ms: int
    required_role: Role
    audit: bool = False
    # Skip the transport-level namespace-access check. Used by self-service
    # ops where the caller is operating ON a namespace (join, leave,
    # fresh register) rather than IN one — the handler enforces its own
    # rules (membership, visibility, owner). Role check still runs.
    skip_namespace_authz: bool = False
    # The op reads facts about the DEPLOYMENT (its build, its op registry), not any
    # org's or namespace's data. A scoped token — whose authority is per-namespace and
    # which the transport would otherwise refuse with IDENTITY_INCOMPLETE for want of a
    # namespace — passes the role gate with its best per-namespace role.
    deployment_scoped: bool = False
    # The op is addressed by an org-wide id (memory_id,
    # erasure_id, pattern_id) and its handler authorizes the ROW's namespace
    # (`row_in_scope`) once it has the row. A scoped token has
    # no namespace to show the transport, so the gate only asks that it hold
    # the role somewhere in the org; the handler decides, and hides a foreign
    # row as not found.
    row_scoped: bool = False
    # The op changes stored state although a VIEWER may call it and a retry
    # is safe (a feedback row, a watch registration, a seen-marker). Only
    # such ops set it: a WRITER+ role or `audit=True` already says "writes".
    writes: bool = False
    maturity: Maturity = "stable"
    shape_budgets_p99_ms: dict[str, int] = field(default_factory=dict)
    # The MCP tool name when it is not derived from `name`: an op MCP serves
    # through a hand-written synthetic descriptor under a name clients already
    # hold (`search.query` is the `loom_search` tool).
    mcp_tool: str = ""

    @property
    def read_only(self) -> bool:
        """Whether a call changes nothing a reader could observe: idempotent,
        callable at the VIEWER floor, not audited and not marked `writes`.
        The MCP `readOnlyHint` and the `readonly` toolset are this property;
        a client that auto-approves read-only tools relies on it."""
        return (
            self.idempotent
            and self.required_role == Role.VIEWER
            and not self.audit
            and not self.writes
        )

    @property
    def reach(self) -> frozenset[str]:
        """How a caller reaches this op, derived: MCP tier
        membership from ``server.operations.toolsets`` plus the committed
        scan of the SDK, the examples/dev scripts and the plugin hooks
        (``server.operations.reach``). ``{"http_only"}`` means nothing but a
        raw ``/v1/*`` POST reaches it."""
        from server.operations.reach import op_reach

        return op_reach(self.name)

    @property
    def http_path(self) -> str:
        return f"/v1/{self.name}"

    @property
    def mcp_tool_name(self) -> str:
        return self.mcp_tool or f"loom_{self.name.replace('.', '_')}"
