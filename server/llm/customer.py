"""The customer a model call is made for, as headers a metering gateway reads.

With `LOOM_INFERENCE_CUSTOMER_HEADER` set, every chat, embedding, rerank and
classify request names its customer, the org whose data it carries, and
(with `LOOM_INFERENCE_FEATURE_HEADER`) the feature that made it. A request
takes its org from the verified token, through the identity the transports
record for every op (`observability.genai`); a background job or a CLI names
the org of the rows it works on with `for_org`, never inheriting a worker's
identity; a call for the deployment itself (its
startup probe, calibration, a provider check) runs under `system()` and names
`LOOM_INFERENCE_SYSTEM_CUSTOMER`. A call that names none is refused
(`Unattributed`), never sent to be billed to no one: one call never carries two
customers, so a job batching several orgs' rows splits them first.

An org an admin switched off (`org.set_model_calls`, loom.org_model_calls)
gets no model call at all, header or not, and no cached vector or answer
either: the switch is a kill switch. `require()`, at the top of every
sender, checks both before the gateway token, the caches or provider health.
Each process tries to read the switched-off orgs at most every
`LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS` and refuses every call for an org
(`BackendUnavailableError`) once its copy is older than
`LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`: the switch fails closed. A call that
names an org is checked even as the deployment's (`system()`); only a
deployment call that names none is never switched off.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from collections.abc import Iterable, Iterator
from urllib.parse import quote

from server.core.errors import BackendUnavailableError, LoomServerError
from server.observability import genai
from server.settings import settings

# The org a background job is working for; a request's comes from its identity.
_org: contextvars.ContextVar[str] = contextvars.ContextVar("loom_model_call_org", default="")
_system: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "loom_model_call_system", default=False
)

# Not an org: the identity tuple's "none", and an org-wide token's wildcard.
_NO_ORG = ("", "-", "*")

log = logging.getLogger(__name__)
# A missed call path is logged once a minute per feature, not on every call.
_LOG_EVERY_S = 60.0
_logged_at: dict[str, float] = {}


class Unattributed(LoomServerError):
    """A model call with no customer to bill: a caller that set none (a job
    that did not name the org of its rows). Refused, never retried."""

    code = "MODEL_CALL_UNATTRIBUTED"
    status_code = 500


class ModelCallsDisabled(LoomServerError):
    """The org's admin switched its model calls off: nothing is sent for it."""

    code = "MODEL_CALLS_DISABLED"
    status_code = 403


# The orgs switched off, as this process last read them, when (monotonic; 0.0 =
# never read), and when the next read is due (a failed read waits its turn too,
# so the waiters do not each query in turn).
_off: frozenset[str] = frozenset()
_read_at = 0.0
_next_read = 0.0
_lock = asyncio.Lock()


@contextlib.contextmanager
def for_org(org: str) -> Iterator[None]:
    """Model calls made inside this block are for `org`."""
    token = _org.set(org)
    try:
        yield
    finally:
        _org.reset(token)


@contextlib.contextmanager
def system() -> Iterator[None]:
    """Model calls made inside this block are the deployment's own."""
    token = _system.set(True)
    try:
        yield
    finally:
        _system.reset(token)


def current() -> str:
    """The org the current model call is for; "" when none is known. A
    request's identity counts, a worker's does not: a pass stamps the org of
    the row it last worked on, which the next row's call must not inherit."""
    org = _org.get()
    if org in _NO_ORG:
        op = genai.current_operation()
        org = genai.current_org() if op and not op.startswith("worker.") else ""
    return "" if org in _NO_ORG else org


async def require() -> str:
    """The customer the current model call is for, checked before anything is
    sent, cached or recorded: "" with no customer header configured. Raises
    ModelCallsDisabled when the call's org is switched off (header or not),
    and Unattributed, logged, when a header is configured and the call names
    no org."""
    org = current()
    if org and await switched_off(org):
        raise ModelCallsDisabled(f"model calls are switched off for org {org!r}")
    if not settings.inference_customer_header:
        return ""
    if org:
        return org
    if _system.get():
        return settings.inference_system_customer
    what = feature()
    # Logged here: a fail-open caller (a reranker, a degraded leg) may swallow
    # the error, and the missed path must still show by its code.
    now = time.monotonic()
    if now - _logged_at.get(what, -_LOG_EVERY_S) >= _LOG_EVERY_S:
        _logged_at[what] = now
        log.error("MODEL_CALL_UNATTRIBUTED: a model call (%s) names no customer", what)
    raise Unattributed(
        f"a model call ({what}) names no customer: run it for an org "
        "(server.llm.customer.for_org) or as the deployment's (customer.system)"
    )


def headers(org: str) -> dict[str, str]:
    """The customer and feature headers for one model request, for the org
    `require()` returned; {} when it returned none (no header configured)."""
    if not org:
        return {}
    # Header values are latin-1 with no line breaks; an org id never needs more.
    out = {settings.inference_customer_header: quote(org, safe="-._~:@")}
    if settings.inference_feature_header:
        out[settings.inference_feature_header] = quote(feature(), safe="-._~:")
    return out


def feature() -> str:
    """What made the call: the cascade stage when one is set (hyde,
    extract_typed, ...), else the op or worker (memory.search,
    worker.embedding)."""
    stage = genai.current_stage()
    if stage != "other":
        return stage
    return genai.current_operation() or "other"


def by_org(orgs: Iterable[str]) -> dict[str, list[int]]:
    """The positions of `orgs` grouped by org, in first-seen order: one model
    call per group, since a call names a single customer."""
    out: dict[str, list[int]] = {}
    for i, org in enumerate(orgs):
        out.setdefault(org, []).append(i)
    return out


async def switched_off(org: str) -> bool:
    """Whether `org`'s model calls are switched off. Raises
    BackendUnavailableError when this process's copy of the switch is too old
    to trust."""
    return org in await switched_off_orgs()


async def switched_off_orgs() -> frozenset[str]:
    """The orgs whose model calls are switched off, for a job to pass over;
    raises BackendUnavailableError when this process's copy is too old."""
    if time.monotonic() >= _next_read:
        async with _lock:
            if time.monotonic() >= _next_read:
                await _read()
    if not _read_at or time.monotonic() - _read_at > settings.jwt_revocation_max_stale_seconds:
        raise BackendUnavailableError(
            "the model-call switch could not be read; model calls wait until it is"
        )
    return _off


async def _read() -> None:
    global _off, _read_at, _next_read
    from server.core.ch import get_client

    _next_read = time.monotonic() + settings.model_calls_policy_refresh_seconds
    try:
        # Every org switched off, one row each: never cut short, which would fail open.
        rows = await get_client().query_all(
            "SELECT org FROM loom.org_model_calls FINAL WHERE enabled = 0"
        )
    except Exception as e:  # noqa: BLE001 — the last copy serves until it is too old
        log.warning("model-call switch: read failed (%s); serving the last copy", e)
        return
    _off, _read_at = frozenset(str(r["org"]) for r in rows), time.monotonic()


def forget_switch() -> None:
    """Make the next call read the switch again, for a change just made here.
    The copy in hand keeps its age, so a failed read serves it as before."""
    global _next_read
    _next_read = 0.0


def _reset_for_tests(off: frozenset[str] = frozenset(), *, read: bool = True) -> None:
    global _off, _read_at, _next_read
    now = time.monotonic()
    _off, _read_at = off, (now if read else 0.0)
    _next_read = now + settings.model_calls_policy_refresh_seconds if read else 0.0
