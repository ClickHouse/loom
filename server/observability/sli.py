"""The service-level columns a request's ledger row carries.

`slo_class` groups operations the way their SLOs are set, `status_class` says
whose fault a failure was, and `budget_ms` is the latency budget the request
was held to (`budgets.effective_budget_ms`). `loom_telemetry.sli_minute` rolls
the rows up per minute; the alert rules and the SLO tiles read that table.
"""

from __future__ import annotations

from functools import cache

SLO_CLASSES = ("write", "search", "read", "code", "admin")
STATUS_CLASSES = ("ok", "client", "server")

# Retrieval ops that are reads by role but carry the search SLO.
_SEARCH_OPS = frozenset(
    {
        "memory.search",
        "memory.search_trace",
        "memory.ask",
        "memory.brief",
        "knowledge.search",
        "knowledge.synthesize",
        "search.query",
        "conversation.search",
        "context.pack",
        "context.fetch",
        "memory.recall",
        "procedure.match",
        "tracker.similar",
        "tracker.duplicate_check",
    }
)

# Refusals the transports record without the exception that raised them,
# counted as the caller's. Each answers a 4xx but IDEMPOTENT_IN_PROGRESS, a 503
# with Retry-After that a client causes by retrying a key still running.
CLIENT_CODES = frozenset(
    {
        "AUTH_INVALID",
        "BAD_REQUEST",
        "HANDLER_HTTP_ERROR",
        "IDEMPOTENT_CONFLICT",
        "IDEMPOTENT_IN_PROGRESS",
        "NAMESPACE_NOT_ACCESSIBLE",
        "OP_NOT_ON_MCP",
        "ORG_MISMATCH",
        "POLICY_FORBIDDEN",
        "UNKNOWN_ARGUMENT",
    }
)


def slo_class(name: str, *, writes: bool, admin: bool) -> str:
    """`code`, `search`, `admin`, `write` or `read`, first match wins.
    `writes` and `admin` come from the op's required role."""
    if name.startswith("code."):
        return "code"
    if name in _SEARCH_OPS:
        return "search"
    if admin:
        return "admin"
    return "write" if writes else "read"


@cache
def _coded_statuses() -> dict[str, int]:
    """Every LoomServerError code and its HTTP status."""
    from server.core.errors import LoomServerError

    out: dict[str, int] = {}
    stack: list[type[LoomServerError]] = [LoomServerError]
    while stack:
        cls = stack.pop()
        out[cls.code] = int(cls.status_code)
        stack.extend(cls.__subclasses__())
    return out


def status_class(success: bool, error_code: str, error: BaseException | str | None) -> str:
    """`ok`, `client` (a 4xx: the caller's request was refused) or `server`.
    A failure whose status is unknown counts against the server."""
    if success:
        return "ok"
    status = getattr(error, "status_code", None) if isinstance(error, BaseException) else None
    if not isinstance(status, int):
        status = _coded_statuses().get(error_code)
    if status is None and (error_code in CLIENT_CODES or error_code.startswith("HTTP_4")):
        status = 400
    return "client" if status is not None and 400 <= status < 500 else "server"
