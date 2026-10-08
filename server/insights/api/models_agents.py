"""agents.* — visibility into which AI clients are talking to Loom.

Each row in `loom.agent_events` carries an `agent` field set by the
caller's JWT (or MCP transport header). This file exposes a rollup so
the dashboard can show "which clients have written recently, when, to
which namespaces." The use case is the silence-after-restart anxiety:
a user pastes the MCP config into Claude Desktop, restarts, and wants
one place to confirm Loom is hearing it.

VIEWER-readable. No mutations.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AgentsConnectedInput(BaseModel):
    org: str
    # Empty → org-wide rollup across every namespace the caller can see.
    namespace: str | None = None
    # How far back to look. 24h is the right default for the "did my
    # restart take effect" check; bump up for trend-style inspection.
    window_hours: int = Field(default=24, ge=1, le=720)
    # The sidebar's badge wants one number — how many
    # agents were first seen inside the window and did something — not every
    # agent's row. One aggregate, no rows; hidden from the MCP listing.
    count_only: bool = False


class ConnectedAgent(BaseModel):
    agent: str
    last_seen: datetime
    # Earliest observation of this agent in agent_events within the last
    # 30 days (bounded so a badge tick is not a whole-history scan). The UI
    # uses this to flag genuinely-new MCP
    # clients — `first_seen` in the last 24h means "this is a fresh
    # connection," vs `last_seen` in the last 24h which means "active
    # recently regardless of how long it's been around." An agent idle for
    # a month reads as new when it returns.
    first_seen: datetime
    ops: int
    namespaces_touched: int
    # The namespace this agent writes to most in the window. Used in
    # the UI when window-scoped to a single namespace this echoes that
    # namespace; org-wide it's the "where this client is most active".
    primary_namespace: str
    # Sum of upstream LLM cost attributed to ops this agent issued —
    # tells the operator which clients are the cost drivers.
    cost_usd: float = 0.0


class AgentsConnectedOutput(BaseModel):
    # One row per agent seen in the window, most recently seen first; empty with `count_only`
    # or when the caller can read no namespace in the org.
    rows: list[ConnectedAgent]
    # The lookback the rows cover, in hours (echoes the request).
    window_hours: int
    # With `count_only`, the number of agents first
    # seen inside the window with at least one op; None otherwise.
    new_count: int | None = None
