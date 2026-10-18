"""The service-level columns a request's ledger row carries.

`slo_class` groups operations the way their SLOs are set, `status_class` says
whose fault a failure was, and `budget_ms` is the latency budget the request
was held to (`budgets.effective_budget_ms`). `loom_telemetry.sli_minute` rolls
the rows up per minute for SLO dashboards and alert rules to read.
"""

from __future__ import annotations

# Ops that rank results against a query (most embed it), build a recall
# bundle from such a ranking, or answer with the LLM over what they retrieved
# or were named (memory.ask, knowledge.synthesize) carry the search SLO: an
# embedder or LLM outage burns it, not the read SLO of the point reads. A
# read by key alone is not one.
_SEARCH_OPS = frozenset(
    {
        "memory.search",
        "memory.search_trace",
        "memory.ask",
        "memory.brief",
        "memory.stack",
        "memory.demonstrate",
        "knowledge.search",
        "knowledge.synthesize",
        "search.query",
        "conversation.search",
        "context.pack",
        "memory.recall",
        "procedure.match",
        "decision.precedents",
        "namespace.search_categories",
        "tracker.similar",
        "tracker.duplicate_check",
    }
)

# How long the SLI rollup leaves a ledger row before folding it, seconds:
# longer than a ledger insert may run (LOOM_CH_TIMEOUT_WRITE_S), so a slow
# insert commits before the rollup's mark passes it. Migration 043 holds the
# same number.
FOLD_MARGIN_S = 70

# Refusals a transport records by code alone, counted as the caller's: MCP's
# refusals before dispatch (server.transport.mcp_tools.emit_refusal) and the
# transports' other literal codes. Each answers a 4xx but
# IDEMPOTENT_IN_PROGRESS, a 503 with Retry-After that a client causes by
# retrying a key still running. HANDLER_HTTP_ERROR is not one: its status is
# the handler's, read from the exception recorded with it.
CLIENT_CODES = frozenset(
    {
        "IDEMPOTENT_CONFLICT",
        "IDEMPOTENT_IN_PROGRESS",
        "NAMESPACE_NOT_ACCESSIBLE",
        "OP_NOT_ON_MCP",
        "ORG_MISMATCH",
        "POLICY_FORBIDDEN",
        "UNKNOWN_ARGUMENT",
        "UNKNOWN_NAMESPACE",
        "UNKNOWN_OP",
    }
)


def slo_class(name: str, *, writes: bool, admin: bool) -> str:
    """`code`, `search`, `admin`, `write` or `read`, first match wins.
    `writes` is any op that is not read-only (some write at the viewer floor,
    such as `namespace.restore`); `admin` comes from the op's required role."""
    if name.startswith("code."):
        return "code"
    if name in _SEARCH_OPS:
        return "search"
    if admin:
        return "admin"
    return "write" if writes else "read"


def _coded_statuses() -> dict[str, int]:
    """Every LoomServerError code and its HTTP status. Walked on each failure,
    not cached: a subclass imported later must not read as the server's."""
    from server.core.errors import LoomServerError

    out: dict[str, int] = {}
    stack: list[type[LoomServerError]] = [LoomServerError]
    while stack:
        cls = stack.pop()
        out[cls.code] = int(cls.status_code)
        stack.extend(cls.__subclasses__())
    return out


def status_class(success: bool, error_code: str, error: BaseException | str | None) -> str:
    """`ok`, `client` (a 4xx, or a retried idempotency key still running) or `server`.
    A failure whose status is unknown counts against the server."""
    if success:
        return "ok"
    # A policy denial no enforcing rule made is a write refused because the
    # policy could not be evaluated (fail closed): the server's failure.
    decision = getattr(error, "decision", None)
    if decision is not None and not decision.enforcing_rules:
        return "server"
    # So is an access refusal made because a read it needed failed (a grant,
    # an account, a classification: server.auth.fail_closed), whatever error
    # it was raised as.
    cause = error if isinstance(error, BaseException) else None
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:  # a chain can loop
        if getattr(cause, "unverified", False):
            return "server"
        seen.add(id(cause))
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    status = getattr(error, "status_code", None) if isinstance(error, BaseException) else None
    if not isinstance(status, int):
        status = _coded_statuses().get(error_code)
    if status is None and (error_code in CLIENT_CODES or error_code.startswith("HTTP_4")):
        status = 400
    return "client" if status is not None and 400 <= status < 500 else "server"
