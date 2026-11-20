"""A Loom refusal is a tool result, not a JSON-RPC error.

A handler refuses by raising an HTTP-style error: 423 LOCKED, 409
PINNED_CONFLICT or a task-state code, 403 TASK_SELF_ACCEPT, 422 BLACKLISTED,
404 RESOURCE_NOT_FOUND, 429. As a JSON-RPC error frame (`-32000 "tool
execution failed: 423: {...}"`) such a refusal loses its guidance (who can
act, what to do instead): Codex shows only "tool call failed … Mcp error",
and the ledger would record it as INTERNAL_ERROR.

`refusal_body(e)` classifies: a 4xx other than 401 is a refusal and comes back
as `isError: true` with the structured body. A 401 (the session must
re-authenticate) and a 5xx (the server failed) stay transport errors; a 5xx
Loom raised on purpose (`server_failure`) keeps its code and retry hint there.
"""

from __future__ import annotations

from typing import Any

# A 401 means the credential itself failed; the client must re-authenticate,
# so it stays a transport error. 5xx is a server failure.
_NOT_REFUSALS = frozenset({401})


def _status_and_detail(e: BaseException) -> tuple[int, Any, dict[str, str]] | None:
    from server.core.errors import LoomServerError

    if isinstance(e, LoomServerError):
        return int(e.status_code), e.to_detail(), {}
    status = getattr(e, "status_code", None)
    if isinstance(status, int) and hasattr(e, "detail"):  # fastapi/starlette HTTPException
        return status, getattr(e, "detail", None), dict(getattr(e, "headers", None) or {})
    return None


def refusal_body(e: BaseException) -> dict[str, Any] | None:
    """The structured refusal for a handler's 4xx, or None when `e` is not
    one (a bug, a 5xx, a 401)."""
    got = _status_and_detail(e)
    if got is None:
        return None
    status, detail, headers = got
    if not 400 <= status < 500 or status in _NOT_REFUSALS:
        return None
    if isinstance(detail, dict):
        body: dict[str, Any] = dict(detail)
    else:
        body = {"message": str(detail or "")}
    body.setdefault("code", _default_code(status))
    body.setdefault("message", "")
    body["status"] = status
    return _with_retry_after(body, headers)


def _with_retry_after(body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """`body` with the `Retry-After` header as `retry_after_s`, which MCP has no header for."""
    retry = headers.get("Retry-After") or headers.get("retry-after")
    if retry and "retry_after_s" not in body:
        try:
            body["retry_after_s"] = max(1, int(float(retry)))
        except ValueError:
            pass
    return body


def server_failure(e: BaseException) -> dict[str, Any] | None:
    """A 5xx the server raised with a code (a LoomServerError such as OVERLOADED or
    BACKEND_TIMEOUT, or an HTTPException whose detail names one): its wire detail plus
    `status`, the body HTTP answers with. None for anything else. It stays a JSON-RPC
    error, but one that names its code and hint instead of reading as an internal error."""
    got = _status_and_detail(e)
    if got is None:
        return None
    status, detail, headers = got
    if status < 500 or not isinstance(detail, dict) or not detail.get("code"):
        return None
    return _with_retry_after({**detail, "status": status}, headers)


def refusal_text(body: dict[str, Any]) -> str:
    """One line for the text block: code, message, and the next step."""
    line = f"{body.get('code')}: {body.get('message') or ''}".strip()
    nxt = body.get("next") or body.get("instead")
    if isinstance(nxt, str) and nxt and nxt not in line:
        line += f" Next: {nxt}"
    return line


def _default_code(status: int) -> str:
    return {
        400: "BAD_REQUEST",
        403: "AUTH_INSUFFICIENT",
        404: "RESOURCE_NOT_FOUND",
        409: "CONFLICT",
        422: "VALIDATION_FAILED",
        423: "LOCKED",
        429: "RATE_LIMITED",
    }.get(status, f"HTTP_{status}")


def _with_line(block: dict[str, Any], line: str) -> dict[str, Any]:
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        return {**block, "text": block["text"] + line}
    return block


def classify_tool_errors(resp: dict[str, Any]) -> dict[str, Any]:
    """A `tools/call` answer with the error envelope's `category` and `retryable` on
    its refusal body or its JSON-RPC error data, the fields HTTP's `detail` carries.
    A fault carries its code (`INTERNAL_ERROR` for one Loom does not name)."""
    from server.core.errors import classified

    result = resp.get("result")
    if isinstance(result, dict) and result.get("isError"):
        body = result.get("structuredContent")
        if isinstance(body, dict) and body.get("code"):
            classed = classified(body, _status_of(body, 0))
            # The text says it too, for a host that shows the model only the text.
            said = "do not retry unchanged"
            if classed["retryable"]:
                wait = classed.get("retry_after_s")
                said = "retry later" if wait is None else f"retry after {wait} s"
            line = f"\n({classed['category']}: {said})"
            content = [_with_line(c, line) for c in result.get("content") or []]
            answer = {**result, "structuredContent": classed, "content": content}
            return {**resp, "result": answer}
        return resp
    error = resp.get("error")
    if not isinstance(error, dict):
        return resp
    data = error.get("data")
    if isinstance(data, dict) and data.get("code"):
        return {**resp, "error": {**error, "data": classified(data, _status_of(data, 500))}}
    return resp


def _status_of(body: dict[str, Any], default: int) -> int:
    status = body.get("status")
    return status if isinstance(status, int) else default


def refusal_result(e: BaseException) -> dict[str, Any]:
    """A 4xx LoomServerError as an MCP `isError` tool result."""
    body = refusal_body(e) or {"code": "BAD_REQUEST", "message": str(e)}
    return {
        "content": [{"type": "text", "text": refusal_text(body)}],
        "isError": True,
        "structuredContent": body,
    }
