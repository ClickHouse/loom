"""A refusal on a code or tracker index says how to get in.

A bare role-check failure ("principal … cannot access acme/code-myrepo")
says neither what to do nor that every other connection and code tool will
refuse the same way, so a model keeps retrying. This module shapes that
refusal: one line naming the principal, the index, and the ways in, plus
`retry: false` so a model (or the plugin) stops. Its `code` is the role
refusal's on both transports (`AUTH_INSUFFICIENT`); `reason: NO_REPO_ACCESS`
names the cause, beside the `namespace` it was refused on. HTTP carries the
same three fields in its 403 detail (`reason_fields`).
"""

from __future__ import annotations

from typing import Any

from server.core.errors import AuthInsufficientError

NO_REPO_ACCESS = "NO_REPO_ACCESS"
_PLANE_TOOLS = ("loom_code_", "loom_tracker_")
_PLANE_OPS = ("code.", "tracker.")


def is_plane_tool(tool_name: str) -> bool:
    return str(tool_name or "").startswith(_PLANE_TOOLS)


def is_plane_op(op_name: str) -> bool:
    return str(op_name or "").startswith(_PLANE_OPS)


def _ways_in(subject: str, namespace: str) -> str:
    if subject.startswith("sa:agent-"):
        return (
            "this is the machine's self-signup token, which has no grants of its own: claim "
            "the namespace at /me with the code in ~/.loom/claim-*.txt so it acts as you, or "
            "save a token for your user (`loom mint-token --save`)"
        )
    return f"request access to {namespace!r} at /me → Discover, or ask its admin for a grant"


def refusal(subject: str, org: str, namespace: str) -> dict[str, Any]:
    """The structured body of a code/tracker access refusal."""
    where = f"{org}/{namespace}" if org else namespace
    msg = (
        f"No access: {subject or 'this token'} cannot read the index {where}. Every "
        f"code/tracker tool on it, on any Loom connection using this credential, will refuse "
        f"the same way — do not retry; fall back to local grep/Read. To get access: "
        f"{_ways_in(subject or '', namespace)}."
    )
    return {"error": msg, "code": AuthInsufficientError.code, **reason_fields(namespace)}


def reason_fields(namespace: str) -> dict[str, Any]:
    """What a code/tracker index refusal adds to the role refusal, on MCP and
    HTTP alike: the cause, the namespace, and that retrying will not help."""
    return {"reason": NO_REPO_ACCESS, "namespace": namespace, "retry": False}


def mcp_result(body: dict[str, Any]) -> dict[str, Any]:
    """The MCP tool result for a refusal body (isError, text = the message)."""
    return {
        "content": [{"type": "text", "text": body["error"]}],
        "isError": True,
        "structuredContent": body,
    }
