"""The MCP transport: tool listing, lean results, session runs."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.settings._env import CaseFolded


class McpSettings(BaseModel):
    # loom_search drops knowledge hits below this cosine similarity (the
    # conversation leg's floor, applied to the knowledge leg).
    mcp_loom_search_knowledge_min_similarity: float = 0.30
    # loom_search's profile leg: sections the words missed pass by cosine
    # against `label\nvalue` at this hard floor. 0 = lexical only.
    mcp_loom_search_profile_min_similarity: float = 0.38
    # The soft floor (a question about the user only).
    mcp_loom_search_profile_soft_similarity: float = 0.30
    # The soft tier's lead over the namespace's runner-up.
    mcp_loom_search_profile_margin: float = 0.08

    # Sub-floor rows in loom_search: "drop" serves only what clears the floor;
    # "readmit" (the memory.search default) pads top_k with below-threshold
    # rows the reader is told to ignore; "ids_only" pads with empty bodies.
    mcp_loom_search_floor_mode: Literal["drop", "readmit", "ids_only"] = "drop"
    # Precision floor for loom_search memory hits. Negative = auto (midpoint
    # of the embedder's calibrated noise floor and question→fact p25);
    # 0 disables.
    mcp_loom_search_precision_floor: float = -1.0
    # Extra margin over the precision floor for hits from another namespace.
    mcp_loom_search_foreign_margin: float = 0.08
    # MCP transport auth mode (the endpoint exposes one tool per wire op, so an
    # anonymous prober could otherwise enumerate every tool):
    #   "required"        — every JSON-RPC method requires a bearer.
    #   "discovery_only"  — initialize/tools/list/ping open; tools/call needs auth.
    #   "open"            — no auth on any method. Trusted-network only.
    # Any other value fails at load: a mistyped mode must not boot as some mode.
    mcp_auth_mode: Literal["required", "discovery_only", "open"] = "required"

    # Which tools `tools/list` shows — VISIBILITY only; every op stays callable
    # by name over MCP and /v1/*. Rosters are the frozensets in
    # server/operations/toolsets.py plus loom_search and loom_activate.
    #   "core"   — (default) _MCP_CORE_OPS (+ _MCP_CODE_CORE_OPS when
    #              mcp_code_tools is on).
    #   "coding" — _MCP_CODING_OPS + _MCP_CODE_CORE_OPS; the plugin selects it
    #              per request with `?toolset=coding`.
    #   "all"    — every op except _MCP_HIDDEN_OPS and the experimental ones
    #              no curated tier lists (they stay callable by name).
    #   "atomic" — CRUD + scratchpad + run ledger (_MCP_ATOMIC_OPS),
    #              policy-shaped descriptions.
    # An unknown value is "core".
    mcp_toolset: str = "core"
    # tools/list schema profile: "standard" (curated prose + inputSchema),
    # "full" (+ outputSchema) or "compact" (first paragraph, argument tree).
    # Per request: `?schema_profile=`.
    mcp_schema_profile: Annotated[Literal["standard", "full", "compact"], CaseFolded] = "standard"
    # Ops whose tools/list entry carries `_meta: {"anthropic/alwaysLoad": true}`, so a host
    # that defers MCP tools behind tool search lists these up front and an agent calls them
    # on its first turn instead of spending a turn loading them. Comma-separated op names;
    # empty = none.
    mcp_always_load_tools: str = "code.search,code.symbol"
    # Seconds without a tool call after which an MCP session's implicit run (one
    # per token and `Mcp-Session-Id`, begun on the first run-scoped tool call) is
    # closed.
    mcp_session_run_idle_seconds: int = Field(default=1800, ge=1)
    # A rate-limited tool call waits out its Retry-After once and re-checks
    # before returning RATE_LIMITED (MCP clients don't retry). This is the
    # longest Retry-After, seconds, that one retry waits out; a longer hint is
    # refused at once, and 0 refuses every limited call at once.
    mcp_ratelimit_retry_max_wait_s: float = 2.0

    # List code.* tools on the MCP surface (core toolset union). Off = code
    # ops are HTTP/SDK-only.
    mcp_code_tools: bool = False
