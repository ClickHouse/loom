"""Operation dataclass — the load-bearing pattern of the Control Plane.

HTTP/JSON and MCP transports both derive from the same Operation declaration.
Adding an operation = creating one declaration; both transports auto-generate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Literal, get_args

from pydantic import BaseModel
from pydantic.config import JsonDict

from server.settings.catalog import OP_BUDGETS_P99_MS, OP_SHAPE_BUDGETS_P99_MS


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


def rank_role(rank: int) -> Role | None:
    """The role a grant of `rank` gives; None for 0 or any other value."""
    return Role[GRANT_ROLES[rank - 1].upper()] if 1 <= rank <= len(GRANT_ROLES) else None


# A handler takes its own operation's input model, which the transports
# validate before the call. Callable parameters are contravariant, so no
# single model type can name "the op's input" across a mixed registry.
HandlerFn = Callable[[Any], Awaitable[BaseModel]]

# Maturity of an operation on the wire, set by hand from the reach census.
# ``stable`` ops are listed under MCP ``all``; op_reach_census.py --check fails
# one with no reach path.
# The label is set by hand, from what the 30-day reach census reports.
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
        required_role: minimum role required to call this operation.
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
    # The MCP tool name when it is not derived from `name`: an op MCP serves
    # through a hand-written synthetic descriptor under a name clients already
    # hold (`search.query` is the `loom_search` tool).
    mcp_tool: str = ""
    # What an `audit=True` row records, from the input and the response; None records
    # the input as `audit.input_subject` shapes it (naming fields as sent, other text
    # hashed). `loom.audit_log` outlives erasures, so an op whose input carries a
    # credential leaves it out.
    audit_subject: Callable[[Any, Any], dict[str, Any]] | None = None

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
        scan of the SDK, the examples/dev scripts, the plugin hooks and the
        dashboard (``server.operations.reach``). ``{"http_only"}`` means nothing but a
        raw ``/v1/*`` POST reaches it."""
        from server.operations.reach import op_reach

        return op_reach(self.name)

    @property
    def budget_p99_ms(self) -> int:
        """The design-target p99 latency, from the budgets table in
        `server/settings/catalog.py`. A successful handler that runs longer emits a
        `budget.violated` event; `effective_budget_ms` applies the deployment's
        overrides and multiplier."""
        return OP_BUDGETS_P99_MS[self.name]

    @property
    def shape_budgets_p99_ms(self) -> dict[str, int]:
        """p99 budgets for the request shapes the handler notes at run time
        (`server.observability.budget_shape`), `{shape: ms}`; the catalogue
        (`/v1/operations`) lists them so a client can size its own timeouts."""
        return dict(OP_SHAPE_BUDGETS_P99_MS.get(self.name, {}))

    @property
    def http_path(self) -> str:
        return f"/v1/{self.name}"

    @property
    def mcp_tool_name(self) -> str:
        return self.mcp_tool or f"loom_{self.name.replace('.', '_')}"
