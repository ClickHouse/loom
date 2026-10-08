"""Which leg of a duplicate check dropped, why, and whether one more try may help.

A leg's read fails as a `TrackerUnavailable`; the exception chain says what
happened (our own timeout, a connection error, a ClickHouse code, an upstream
HTTP status, an open breaker, a refused statement). `classify` turns it into a
short reason code, a fixed message (never the upstream text, which may carry
SQL or a URL) and whether the failure is transient. The deadline is the op's
latency budget, started when the call arrives; a retry runs only inside it.
Pure half: the handler runs the reads (`server.tracker.app.dupcheck`).
"""

from __future__ import annotations

import contextvars
import re
import time
from dataclasses import dataclass
from typing import Any

# The legs a check reports, in answer order.
LEGS: tuple[str, ...] = ("lexical", "frames", "mentions", "vector", "fix_path")

# ClickHouse codes that name the network or the clock, not the statement.
TRANSIENT_CH_CODES: frozenset[int] = frozenset(
    {
        159,  # TIMEOUT_EXCEEDED
        209,  # SOCKET_TIMEOUT
        210,  # NETWORK_ERROR
        1000,  # POCO_EXCEPTION (connection reset, refused)
    }
)
_CH_CODE_NAMES = {
    "TIMEOUT_EXCEEDED": 159,
    "SOCKET_TIMEOUT": 209,
    "NETWORK_ERROR": 210,
    "POCO_EXCEPTION": 1000,
}
_CODE_RE = re.compile(r"[Cc]ode: (\d+)")
_HTTP_RE = re.compile(r"HTTP status (\d{3})|response code (\d{3})")
_OWN_TIMEOUT_RE = re.compile(r"exceeded \d+ ms")

MESSAGES: dict[str, str] = {
    "timeout": "the read ran past its time budget",
    "connection": "the tracker source could not be reached",
    "upstream_5xx": "the tracker source answered with a server error",
    "ch_transient": "the tracker source reported a transient error",
    "breaker_open": "the tracker source is paused after repeated failures",
    "quota": "the tracker source refused the read under its quota",
    "shape": "the tracker source cannot run this read",
    "unconfigured": "the tracker namespace is not configured to serve this read",
    "unavailable": "the tracker source did not answer",
    "retry_skipped_budget": "not retried: too little of the call's time budget was left",
}
_MESSAGE_MAX = 160


@dataclass(frozen=True)
class Failure:
    reason: str
    message: str
    transient: bool


def _chain(e: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    cur: BaseException | None = e
    while cur is not None and len(out) < 6 and cur not in out:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _ch_code(x: BaseException) -> int:
    code = getattr(x, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    text = str(x)
    m = _CODE_RE.search(text)
    if m:
        return int(m.group(1))
    return next((c for name, c in _CH_CODE_NAMES.items() if name in text), 0)


def _http_status(x: BaseException) -> int:
    for attr in ("status_code", "status"):
        v = getattr(x, attr, None)
        if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599:
            return v
    m = _HTTP_RE.search(str(x))
    return int(m.group(1) or m.group(2)) if m else 0


def _fail(reason: str, transient: bool, detail: str = "") -> Failure:
    msg = MESSAGES[reason] + (f" ({detail})" if detail else "")
    return Failure(reason, msg[:_MESSAGE_MAX], transient)


def classify(e: BaseException) -> Failure:
    """The reason code of a failed leg read and whether a retry may help. The
    message is built from fixed text and a number (a code, a status), never
    from the upstream's own words."""
    from server.tracker.store.source import qa_intel

    chain = _chain(e)
    for x in chain:
        if isinstance(x, TimeoutError):
            return _fail("timeout", True)
        if isinstance(x, ConnectionError) or type(x).__name__ in (
            "ClientConnectionError",
            "ClientConnectorError",
            "ServerDisconnectedError",
            "ConnectError",
            "RemoteProtocolError",
        ):
            return _fail("connection", True)
        code = _ch_code(x)
        if code in TRANSIENT_CH_CODES:
            reason = "timeout" if code in (159, 209) else "ch_transient"
            return _fail(reason, True, f"code {code}")
        status = _http_status(x)
        if 500 <= status <= 599:
            return _fail("upstream_5xx", True, f"HTTP {status}")
    text = " ".join(str(x) for x in chain)
    if _OWN_TIMEOUT_RE.search(text):
        return _fail("timeout", True)
    if "breaker is OPEN" in text:
        return _fail("breaker_open", False)
    if qa_intel.is_quota_error(e) or any(qa_intel.is_quota_error(x) for x in chain):
        return _fail("quota", False)
    if any(qa_intel.is_shape_error(x) for x in chain):
        return _fail("shape", False)
    if any(
        m in text
        for m in (
            "not a configured tracker namespace",
            "is not configured",
            "LIVE_SERVE is off",
            "configured restricted",
            "served only through a restricted",
        )
    ):
        return _fail("unconfigured", False)
    if any(isinstance(x, OSError) for x in chain):
        return _fail("connection", True)
    return _fail("unavailable", False)


# --- the op's deadline ------------------------------------------------------

_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "tracker_dupcheck_deadline", default=None
)
_DECLARED_MS = 2000


def op_budget_ms() -> int:
    """The latency budget this call is held to (overrides and the deployment's
    multiplier applied); the declaration when the lookup fails."""
    try:
        from server.observability.budgets import effective_budget_ms
        from server.operations.registry import OPERATIONS_BY_NAME

        op = OPERATIONS_BY_NAME.get("tracker.duplicate_check")
        declared = int(getattr(op, "budget_p99_ms", 0) or _DECLARED_MS)
        return max(1, effective_budget_ms("tracker.duplicate_check", declared))
    except Exception:  # noqa: BLE001 — a budget lookup never fails the check
        return _DECLARED_MS


def start_deadline() -> contextvars.Token[float | None] | None:
    """Starts the call's deadline unless an outer call already did (a nested
    check keeps the outer deadline)."""
    if _DEADLINE.get() is not None:
        return None
    return _DEADLINE.set(time.monotonic() + op_budget_ms() / 1000.0)


def end_deadline(token: contextvars.Token[float | None] | None) -> None:
    if token is not None:
        _DEADLINE.reset(token)


def deadline(fallback_start: float) -> float:
    """The call's deadline; a check run without one (a direct call) counts
    from `fallback_start`."""
    got = _DEADLINE.get()
    return got if got is not None else fallback_start + op_budget_ms() / 1000.0


@dataclass(frozen=True)
class RetryWindow:
    """How long a retry may take after its pause, or why it may not run."""

    timeout_s: float
    skipped: str = ""
    left_ms: int = 0


def retry_window(
    until: float, *, delay_ms: int, reserve_ms: int, min_ms: int, now: float | None = None
) -> RetryWindow:
    now = time.monotonic() if now is None else now
    left_ms = int((until - now) * 1000) - max(0, reserve_ms)
    usable = left_ms - max(0, delay_ms)
    if usable < max(1, min_ms):
        return RetryWindow(0.0, skipped="retry_skipped_budget", left_ms=max(0, left_ms))
    return RetryWindow(usable / 1000.0, left_ms=left_ms)


# --- the report ---------------------------------------------------------------

_SKIP_REASONS = {
    "skipped": ("not_applicable", "the draft gave this leg nothing to read, or the leg is off"),
    "not_computed": ("not_computed", "the namespace has no vectors yet"),
}


def leg_report(
    status: dict[str, str],
    timings: dict[str, int],
    trace: dict[str, dict[str, Any]],
    errors: dict[str, BaseException] | None = None,
    *,
    namespace: str = "",
) -> list[dict[str, Any]]:
    """One row per leg: `status` ok | degraded | skipped, a `reason` code and
    `message`, `retried`, and the leg's `ms` (both tries when retried); a
    degraded row's `cause` is its read's failure code (the `reason` too, unless
    the retry was skipped: then the reason says why).
    `trace` is what the handler recorded per failed leg: the Failure of its
    last try, `retried`, and a retry that was skipped with its reason; a leg that
    failed after the retry step (the fix-path file read) is classified from `errors`."""
    rows: list[dict[str, Any]] = []
    for name in LEGS:
        raw = status.get(name)
        t = trace.get(name) or {}
        ms = int(timings.get("fix_in_flight" if name == "fix_path" else name, 0) or 0)
        ms += int(t.get("retry_ms") or 0)
        row: dict[str, Any] = {
            "name": name,
            "status": "ok",
            "reason": "",
            "message": "",
            "retried": bool(t.get("retried")),
            "ms": ms,
        }
        if namespace:
            row["namespace"] = namespace
        if raw is None:
            row.update(status="skipped", reason="not_run", message="the leg did not run")
        elif raw == "tracker_unavailable":
            fail: Failure | None = t.get("failure")
            if fail is None and errors and name in errors:
                fail = classify(errors[name])
            reason = fail.reason if fail else "unavailable"
            message = fail.message if fail else MESSAGES["unavailable"]
            row["cause"] = reason
            skip = str(t.get("skipped") or "")
            if skip:
                # the reason says why no second try ran; `cause` keeps the read's failure
                message = f"{message}; {MESSAGES[skip]}"[:_MESSAGE_MAX]
                reason = skip
            row.update(status="degraded", reason=reason, message=message)
        elif raw in _SKIP_REASONS:
            code, message = _SKIP_REASONS[raw]
            row.update(status="skipped", reason=code, message=message)
        elif t.get("retried"):
            row.update(
                reason="recovered_on_retry", message="the first read failed; the retry answered"
            )
        rows.append(row)
    return rows


def all_down(failure: Failure, *, namespace: str = "") -> list[dict[str, Any]]:
    """Every leg degraded by one failure before any leg ran (the vocabulary read,
    the namespace gate)."""
    rows = []
    for name in LEGS:
        row: dict[str, Any] = {
            "name": name,
            "status": "degraded",
            "reason": failure.reason,
            "message": failure.message,
            "retried": False,
            "ms": 0,
            "cause": failure.reason,
        }
        if namespace:
            row["namespace"] = namespace
        rows.append(row)
    return rows
