"""MCP transport — constants and JSON-RPC envelope helpers.

Leaf module: pure constants + tiny envelope helpers, no imports back into
``server.transport.mcp``. The dispatch loop and helper modules import from
here.
"""

from __future__ import annotations

from typing import Any

from server import __version__

MCP_PROTOCOL_VERSION = "2025-11-25"
SERVER_INFO = {"name": "loom", "version": __version__}

# Every protocol revision this transport serves, latest first.
# `initialize` echoes the client's version when it is one of these and answers
# with the first one otherwise (the lifecycle spec's negotiation rule); the
# `MCP-Protocol-Version` header and a request's `_meta` version are checked
# against the same tuple. 2026-07-28 is not here: that revision removes the
# handshake and the session header this transport is built on, so a client
# asking for it is told the versions it can retry with.
MCP_SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

# `_meta` keys the 2026-07-28 revision defines; read on any request so a
# modern client gets a recognised modern error instead of a guess.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Cache hints the list results carry (2026-07-28 `CacheableResult`; harmless
# extra fields to earlier clients, whose result schemas pass unknown keys
# through). The listings vary by URL (`?toolset=`), never by caller.
LIST_TTL_MS = 300_000
PRIMER_TTL_MS = 60_000


def negotiate_protocol_version(requested: Any) -> str:
    """The version to answer an `initialize` with: the requested one when this
    transport serves it, else the latest it serves (lifecycle spec: "the
    server MUST respond with another protocol version it supports … SHOULD be
    the latest")."""
    if isinstance(requested, str) and requested in MCP_SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return MCP_PROTOCOL_VERSION


def protocol_version_supported(version: Any) -> bool:
    return isinstance(version, str) and version in MCP_SUPPORTED_PROTOCOL_VERSIONS


def requested_meta_version(payload: Any) -> str | None:
    """The `_meta["io.modelcontextprotocol/protocolVersion"]` a request carries,
    or None when it carries none (every pre-2026 client)."""
    if not isinstance(payload, dict):
        return None
    params = payload.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    v = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
    return v if isinstance(v, str) else None


def unsupported_version_error(req_id: Any, requested: Any) -> dict[str, Any]:
    """`UnsupportedProtocolVersionError` (-32022): the recognised modern error
    that tells a 2026-07-28 client which versions to retry with."""
    return _err(
        req_id,
        UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version",
        {"supported": list(MCP_SUPPORTED_PROTOCOL_VERSIONS), "requested": requested},
    )


def cache_hints(ttl_ms: int, scope: str) -> dict[str, Any]:
    return {"ttlMs": int(ttl_ms), "cacheScope": scope}


# ============================================================
# MCP client provenance
# ============================================================
#
# Writes are attributed to the actual client (Cursor, Claude Code, and others), not a
# blanket default. The `initialize` handshake's `params.clientInfo.name` is
# the only self-identification the protocol gives us; we slugify it into an
# agent id. The transport is stateless, so we cache the name against the
# `Mcp-Session-Id` header to correlate `initialize` with later `tools/call`
# POSTs. A cache miss falls back to the default agent — never a regression.

_DEFAULT_MCP_AGENT = "claude-desktop"

# Bounded session→agent cache (sessions are short-lived; cap so a churning
# long-running process can't grow it unbounded).
_MCP_SESSION_AGENT_CAP = 4096


# ============================================================
# JSON-RPC envelope helpers
# ============================================================


def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# JSON-RPC reserved error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TOOL_EXEC_FAILED = -32000  # implementation-defined
# MCP-reserved (2026-07-28 error-code allocation): the version the client
# asked for is not one this transport serves.
UNSUPPORTED_PROTOCOL_VERSION = -32022

# Entries one JSON-RPC batch may hold: only tools/call meets the rate limiter, and not
# loom_activate or loom_op's catalog, so the batch length bounds one request's work.
MAX_BATCH_ENTRIES = 64  # not a setting: a protocol bound, not a deployment's capacity

# Id of a transport-level error frame when the request carried none. MCP
# narrows `JSONRPCError.id` to string | number, so `null` is
# never an option.
_TRANSPORT_ERROR_ID = "loom-transport"


# Synthetic tools — not backed by an Operation, defined directly here.
# `loom_activate` is the natural-language activation fallback; it returns
# the same primer text as the `loom` prompt.
_LOOM_ACTIVATE_TOOL_NAME = "loom_activate"
_LOOM_SEARCH_TOOL_NAME = "loom_search"


# MCP tool annotations — hints (not enforcement; authz in _call_tool is the
# security boundary) derived from registry metadata so new ops inherit them.
_DESTRUCTIVE_NAME_TOKENS: frozenset[str] = frozenset(
    {"delete", "remove", "redact", "purge", "erase", "erasure"}
)
# Reads even when the group carries a destructive token (erasure.status /
# erasure.export report on erasure, they don't destroy anything).
_READ_SHAPED_VERBS: frozenset[str] = frozenset({"get", "list", "status", "export", "history"})


# ============================================================
# MCP prompts / resources — discoverable activation surfaces
# ============================================================

_LOOM_PROMPT_NAME = "loom"
_LOOM_PRIMER_RESOURCE_URI = "loom://primer"
