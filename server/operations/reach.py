"""Reach of an operation: every path a caller can take to it.

An op that is declared, described, tested and migrated but reachable from no
listed tool, no SDK method, no example and no plugin hook is dark, and dark
ops go uncalled. This module derives, per op, the set of reach kinds:

``mcp_core``     listed on the default ``core`` MCP toolset (or with the code
                 tools flag) — the chat loop sees it.
``mcp_atomic`` listed on the ``atomic`` toolset.
``mcp_all``      listed under ``LOOM_MCP_TOOLSET=all``
                 (``toolsets.listed_under_all``). Every op not in
                 ``_MCP_HIDDEN_OPS`` is also callable by name.
``sdk_method``   a Python SDK method posts ``/v1/<op>``.
``sdk_example``  a shipped example (``examples/``) or operator script
                 (``dev/``, ``scripts/``) calls it, directly or through an
                 SDK method.
``plugin_hook``  a first-party client that is neither the SDK nor an MCP
                 listing names it: the Claude Code plugin hooks, the gateway
                 and shim integrations (``integrations/``).
``dashboard``    the dashboard posts it: a quoted ``"/v1/<op>"`` call site in
                 ``frontend/src`` (tests and test helpers aside). An op without
                 it is named in ``server.operations.dashboard`` with the reason.
``http_only``    none of the above — a raw ``/v1/*`` POST is the only path.

The MCP kinds derive live from ``server.operations.toolsets``. The file-scan
kinds derive from the repository tree, which a deployed server does not carry:
``scripts/codegen/op_reach_census.py`` scans it and commits the result as
``reach_manifest.STATIC_REACH``; the unit test regenerates it and fails when it
drifts (``--write-manifest`` refreshes it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from server.operations.base import Operation

REACH_KINDS: frozenset[str] = frozenset(
    {
        "mcp_core",
        "mcp_atomic",
        "mcp_all",
        "sdk_method",
        "sdk_example",
        "plugin_hook",
        "dashboard",
        "http_only",
    }
)

# Kinds the file scan produces (everything but the live MCP kinds and the
# fallback), in the order the manifest and the census table print them.
STATIC_KINDS: tuple[str, ...] = ("sdk_method", "sdk_example", "plugin_hook", "dashboard")


def mcp_reach(name: str) -> set[str]:
    """The MCP kinds for ``name``, read live from the toolset rosters."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import (
        _MCP_ATOMIC_OPS,
        _MCP_CODE_CORE_OPS,
        _MCP_CORE_OPS,
        SYNTHETIC_TOOL_OPS,
        listed_under_all,
    )

    op = OPERATIONS_BY_NAME.get(name)
    if op is None or not listed_under_all(op):
        return set()
    if name in SYNTHETIC_TOOL_OPS:  # listed on every toolset
        return {"mcp_all", "mcp_core", "mcp_atomic"}
    out = {"mcp_all"}
    if name in _MCP_CORE_OPS or name in _MCP_CODE_CORE_OPS:
        out.add("mcp_core")
    if name in _MCP_ATOMIC_OPS:
        out.add("mcp_atomic")
    return out


def op_reach(name: str) -> frozenset[str]:
    """Every reach kind for ``name``; ``{"http_only"}`` when there is none."""
    from server.operations.reach_manifest import STATIC_REACH

    kinds = mcp_reach(name) | set(STATIC_REACH.get(name, ()))
    return frozenset(kinds) if kinds else frozenset({"http_only"})


def has_reach(name: str) -> bool:
    """True when something other than a raw HTTP POST reaches the op."""
    return op_reach(name) != frozenset({"http_only"})


def catalogue_facets(op: Operation) -> dict[str, Any]:
    """The additive catalogue fields (``code.index_status``,
    ``ops.capabilities``): ``maturity``, the sorted ``reach`` list, and
    ``toolsets`` — every MCP toolset whose listing
    carries the op (empty for a hidden op)."""
    from server.operations.toolsets import op_toolsets

    return {
        "maturity": op.maturity,
        "reach": sorted(op.reach),
        "toolsets": op_toolsets(op.name),
    }
