"""`loom_op`: one listed tool that reaches every op a roster leaves unlisted.

Loom's rosters list a focused subset of the registry and describe the rest as
"callable by name". The server answers `tools/call` for an unlisted op, but
an MCP client can only call a tool it was shown: Claude Code's ToolSearch
finds listed tools and nothing else. Without a listed route an agent asked
for `tracker.flaky` or `task.pull` reads Loom's source and hand-rolls `curl`
against /v1 with the token from ~/.loom/config.json (10–14 turns a question).

`loom_op {op, arguments}` is rewritten, before authorization, into the
`tools/call` it names, so the target runs through exactly the path a listed
tool runs through: the dispatch-level namespace check, `_call_tool`'s identity
defaults, code scope, `check_authorized`, `ignored_args`, lean results and the
ledger. `loom_op {op: "help", query}` (or an unknown op) answers a compact
catalog: op names, required and optional args and one line of
purpose. Ops hidden from MCP (`_MCP_HIDDEN_OPS`) stay hidden: they are not in
the catalog and the rewritten call meets the same "not available" refusal a
direct call does. A `?toolset=readonly` connection routes read ops only.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.transport.mcp_config import _ok
from server.transport.mcp_ledger import emit_token_refusal

LOOM_OP_TOOL_NAME = "loom_op"
# The code for a name no op (or tool) answers to, direct or through loom_op.
UNKNOWN_OP = "UNKNOWN_OP"
_HELP = {"", "help", "ops", "list", "catalog", "?"}
_CATALOG_LIMIT = 15

DESCRIPTOR: dict[str, Any] = {
    "name": LOOM_OP_TOOL_NAME,
    "description": (
        "Run an op this list doesn't show — tracker.flaky, task.pull, "
        "facts.traverse and any other op a doc or hint calls callable by name. "
        '`op` is the dotted name, `arguments` its arguments; op="help" with '
        "an optional `query` lists matching ops and their args. "
        "Prefer a listed tool when one fits."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "op": {"type": "string"},
            "arguments": {"type": "object"},
            "query": {"type": "string"},
        },
        "required": ["op"],
    },
    # The target op's own output: one object whatever the op (strict clients
    # validate structuredContent against a declared schema).
    "outputSchema": {"type": "object"},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
}


def _ops() -> list[Any]:
    from server.operations.registry import ALL_OPERATIONS

    return [op for op in ALL_OPERATIONS if op.name not in _MCP_HIDDEN_OPS]


def _purpose(op: Any) -> str:
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    text = str(TOOL_DESCRIPTIONS.get(op.name) or op.input_model.__doc__ or "").strip()
    text = re.sub(r"^(WHAT:|\*\*[^*]+\*\*\s*[—-]?)\s*", "", text)
    first = re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0]
    return first[:140]


def _required(op: Any) -> list[str]:
    from server.transport.arg_feedback import required_args

    return required_args(op.input_model)


def _row(op: Any, purpose: str | None = None) -> dict[str, Any]:
    """One catalog row: the op, its required AND optional args (a help call
    names every arg, so the call after it needs no second help), and a line of
    purpose."""
    from server.transport.arg_feedback import optional_args, shaped

    model = op.input_model
    row: dict[str, Any] = {"op": op.name, "required": [shaped(model, n) for n in _required(op)]}
    optional = [shaped(model, n) for n in optional_args(model)]
    if optional:
        row["optional"] = optional
    row["purpose"] = _purpose(op) if purpose is None else purpose
    return row


def _is_read(op: Any) -> bool:
    return bool(op.read_only)


def routes_writes(toolset: str | None) -> bool:
    """Whether `loom_op` on this connection's ``?toolset=`` routes a write op
    (a read-only connection routes read ops only)."""
    return str(toolset or "").lower() != "readonly"


def reachable(op: Any, toolset: str | None) -> bool:
    """Whether `loom_op` on this connection routes ``op``."""
    return _is_read(op) or routes_writes(toolset)


def catalog(
    query: str = "", *, readonly: bool = False, limit: int = _CATALOG_LIMIT
) -> dict[str, Any]:
    """The ops `loom_op` reaches that match any word of `query`, ranked by
    the words their NAME carries (twice) plus those their purpose carries,
    capped at `limit`. "tracker similar flaky test_signal" asks for three
    ops at once, so a word need not match every op."""
    words = [w for w in re.split(r"[^\w.]+", query.lower()) if w]
    hits: list[tuple[int, str, dict[str, Any]]] = []
    for op in _ops():
        if readonly and not _is_read(op):
            continue
        purpose = _purpose(op)
        name_l, purpose_l = op.name.lower(), purpose.lower()
        score = sum(2 * (w in name_l) + (w in purpose_l) for w in words)
        if words and score == 0:
            continue
        row = _row(op, purpose)
        hits.append((-score, op.name, row))
    hits.sort(key=lambda h: (h[0], h[1]))
    rows = [h[2] for h in hits[:limit]]
    return {"ops": rows, "matched": len(hits), "truncated": len(hits) > len(rows)}


# What an agent does instead of an op hidden from MCP. A model that reaches
# for one ("write two facts in one call", a locked key) would otherwise read
# "no op reachable" plus look-alikes and guess: a help call, a grep of the
# source, a wrong argument name.
_INSTEAD: dict[str, str] = {
    "memory.bulk_set": "call loom_memory_set once per fact; a batch saves no round trip here",
    "memory.bulk_get": "call loom_memory_get per key, or loom_memory_list with a key prefix",
    "memory.keys": "call loom_memory_list, which pages the namespace",
    "memory.delete_version": "call loom_memory_delete",
    # An owner-only op names the person, never a route: naming the SDK/HTTP
    # op sends a model probing env vars and token files for a way around it.
    "memory.unlock": (
        "only the user can unlock a memory: leave the key as it is and tell them it is "
        "locked. Don't look for another route, token or credential to unlock it yourself"
    ),
    "memory.search": "call loom_search (memory, profile, knowledge and conversations in one call)",
    "knowledge.search": "call loom_search",
    "conversation.search": "call loom_search",
    "namespace.list": "call loom_namespace_list_mine",
    "run.list": "call run.get with a run id (loom_op)",
    "memory.extraction_status": (
        "call memory.extraction_status_mine (loom_op) with the queue_id each deferred "
        "memory.set_from_messages returned"
    ),
    "erasure.status": "call erasure.list (loom_op); each saga row carries its status",
    "memory.search_trace": (
        "call loom_search with explain=true; each memory hit carries its ranking terms"
    ),
    # Ops a client program calls, not a person: the agent's nearest op, or nothing to call.
    "ui.vitals": "the dashboard posts its own paint timings; nothing to call",
    "code.outcome": "the plugin's hooks report what was done with a code answer; nothing to call",
    "code.coverage.watch": "call code.index_status (loom_op) for what the index covers now",
    "code.coverage.unwatch": "a bot's watch on its own inbox; nothing to call",
    "code.scip_ingest": "a CI job uploads the SCIP index; nothing to call",
    "code.scip_status": "a CI job uploads the SCIP index and polls its batch; nothing to call",
}


_NEEDS_THE_USER = (
    "this needs the user: ask them to do it in the dashboard. Don't look for another "
    "route, token or credential to do it yourself"
)
# For an op without the `dashboard` reach kind (server/operations/reach.py).
_NEEDS_AN_OPERATOR = (
    "this needs the user or an operator, outside this chat. Don't look for another "
    "route, token or credential to do it yourself"
)


def hidden_refusal(name: str) -> dict[str, Any] | None:
    """The refusal for an op that exists but is kept off MCP: that it is not on
    MCP, and what to do instead. None when `name` is not a hidden op."""
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME.get(name)
    if op is None or name not in _MCP_HIDDEN_OPS:
        return None
    default = _NEEDS_THE_USER if "dashboard" in op.reach else _NEEDS_AN_OPERATOR
    return {
        "error": f"{name} exists but is not on this MCP connection",
        "code": "OP_NOT_ON_MCP",
        "instead": _INSTEAD.get(name, default),
    }


def _result(req_id: Any, body: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    import json

    return _ok(
        req_id,
        {
            "content": [{"type": "text", "text": json.dumps(body, default=str)}],
            "isError": error,
            "structuredContent": body,
        },
    )


def route(req: Any, *, toolset: str | None, auth: Any) -> tuple[Any, dict[str, Any] | None]:
    """`(req, None)` unchanged unless it is a `loom_op` call; the rewritten
    `tools/call` for a real op; or `(req, response)` for help / unknown ops.
    `auth` is the verified token (None in open mode) a refusal is ledgered as."""
    if not isinstance(req, dict) or req.get("method") != "tools/call":
        return req, None
    params = req.get("params")
    if not isinstance(params, dict) or params.get("name") != LOOM_OP_TOOL_NAME:
        return req, None
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return req, None  # `_call_tool` answers INVALID_PARAMS
    from server.transport.mcp_tools import assistant_connection

    if assistant_connection(toolset):
        return req, None  # `_call_tool` refuses `loom_op` on the assistant toolset
    name = str(args.get("op") or "").strip()
    readonly = not routes_writes(toolset)
    if name.lower() in _HELP:
        return req, _result(req.get("id"), catalog(str(args.get("query") or ""), readonly=readonly))
    by_name = {op.name: op for op in _ops()}
    op = by_name.get(name) or by_name.get(name.replace("_", ".", 1))
    hidden = hidden_refusal(name)
    if op is None and hidden is not None:
        emit_token_refusal(name, auth, error_code="OP_NOT_ON_MCP")
        return req, _result(req.get("id"), hidden, error=True)
    if op is None or (readonly and not _is_read(op)):
        if op is not None:  # a write on a read-only connection
            emit_token_refusal(op.name, auth, error_code=UNKNOWN_OP)
        near = difflib.get_close_matches(name, list(by_name), n=5, cutoff=0.6)
        body = {
            "error": f"no op {name!r} reachable here"
            + (" on a read-only connection" if op is not None else ""),
            "code": UNKNOWN_OP,
            "did_you_mean": near,
            # The near ops' args ride the refusal, so the retry needs no help
            # call.
            "ops": [_row(by_name[n]) for n in near[:3]],
            "hint": 'loom_op {"op": "help", "query": "<words>"} lists ops',
        }
        return req, _result(req.get("id"), body, error=True)
    raw = args.get("arguments")
    inner: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    rewritten = {**req, "params": {**params, "name": op.mcp_tool_name, "arguments": inner}}
    return rewritten, None
