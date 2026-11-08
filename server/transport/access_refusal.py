"""A refusal on a code or tracker index says how to get in.

A bare role-check failure ("principal … cannot access acme/code-myrepo") says neither
what to do nor that every other connection and code tool will refuse the same way, so a
model keeps retrying. This module shapes that refusal: one line naming the principal, the
index, and the ways in, plus `retry: false` so a model (or the plugin) stops. Its `code`
is the role refusal's on both transports (`AUTH_INSUFFICIENT`); `reason: NO_REPO_ACCESS`
names the cause, beside the `namespace` it was refused on. HTTP carries the same fields in
its 403 detail (`reason_fields`); there, when the check itself could not run, the reason
is `ACCESS_UNVERIFIED` and `retry` is true. Over MCP the refusal is the same either way.
`retry` and the error envelope's `retryable` agree; a client may read either.
"""

from __future__ import annotations

from typing import Any

from server.core.errors import AuthInsufficientError, unverified_class

NO_REPO_ACCESS = "NO_REPO_ACCESS"
# The check itself could not run (a grant could not be read): the caller may hold access.
ACCESS_UNVERIFIED = "ACCESS_UNVERIFIED"
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
            "the namespace in the dashboard with the code in ~/.loom/claim-*.txt so it acts "
            "as you, or "
            "save a token for your user (`loom mint-token --save`)"
        )
    return (
        f"request access to {namespace!r} from the dashboard's Spaces page, "
        "or ask its admin for a grant"
    )


def refusal(subject: str, org: str, namespace: str) -> dict[str, Any]:
    """The structured body of a code/tracker access refusal over MCP. It is the same
    whether or not the check ran: a restricted code namespace is refused before the grant
    read that fails, so a different answer would tell it from one that does not exist."""
    where = f"{org}/{namespace}" if org else namespace
    msg = (
        f"No access: {subject or 'this token'} cannot read the index {where}. Every "
        f"code/tracker tool on it, on any Loom connection using this credential, will refuse "
        f"the same way — do not retry; fall back to local grep/Read. To get access: "
        f"{_ways_in(subject or '', namespace)}."
    )
    return {"error": msg, "code": AuthInsufficientError.code, **reason_fields(namespace)}


def reason_fields(namespace: str, *, unverified: bool = False) -> dict[str, Any]:
    """What a code/tracker index refusal adds to the role refusal, on MCP and
    HTTP alike: the cause, the namespace, and whether retrying can help (only when
    the check could not run)."""
    reason = ACCESS_UNVERIFIED if unverified else NO_REPO_ACCESS
    fields = {"reason": reason, "namespace": namespace, "retry": unverified}
    return {**fields, **unverified_class("AUTH_INSUFFICIENT", unverified)}


def mcp_result(body: dict[str, Any]) -> dict[str, Any]:
    """The MCP tool result for a refusal body (isError, text = the message)."""
    return {
        "content": [{"type": "text", "text": body["error"]}],
        "isError": True,
        "structuredContent": body,
    }
