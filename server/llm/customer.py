"""The customer a model call is made for, as headers a metering gateway reads.

With `LOOM_INFERENCE_CUSTOMER_HEADER` set, every chat, embedding, rerank and
classify request names its customer, the org whose data it carries, and
(with `LOOM_INFERENCE_FEATURE_HEADER`) the feature that made it. A request
names the org it addresses, as authorized for its token, through the identity
the transports record for every op (`observability.genai`); a background job
or a CLI names the org of the rows it works on with `for_org`, never
inheriting a worker's identity; a call for the deployment itself (its startup
probe, the recalibration worker's fixed pairs, a provider check) runs under
`system()` and names `LOOM_INFERENCE_SYSTEM_CUSTOMER`. A call that names none
is refused (`Unattributed`), never sent to be billed to no one: one call never
carries two customers, so a job batching several orgs' rows splits them first.

An org an admin switched off (`org.set_model_calls`, loom.org_model_calls)
gets no model call at all, header or not, and no cached vector or answer
either: the switch is a kill switch. `require()`, at the top of every
sender, checks both before the gateway token, the caches or provider health.
Each process tries to read the switched-off orgs at most every
`LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS` and refuses every call for an org
(`SwitchUnread`, a BackendUnavailableError) once its copy is older than
`LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS`: the switch fails closed. A call that
names an org is checked even as the deployment's (`system()`); only a
deployment call that names none is never switched off.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from collections.abc import Iterable, Iterator
from urllib.parse import quote

from server.core.errors import BackendUnavailableError, LoomServerError
from server.core.polled_copy import PolledCopy
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


class SwitchUnread(BackendUnavailableError):
    """This process's copy of the switch is too old to trust: no call is made
    for an org until it is read, and a job leaves its rows as they are."""


# What require() holds a call with: a refusal, or a switch it cannot read. None
# is a provider fault, so a fail-open caller (a reranker, classify) lets these
# through, and a job leaves its rows as they are to try again later; a queued
# extraction request naming no customer still fails, since it never clears.
HOLDS: tuple[type[LoomServerError], ...] = (Unattributed, ModelCallsDisabled, SwitchUnread)


async def _read() -> frozenset[str]:
    from server.core.ch import get_client, own_write_reads

    # Every org switched off, one row each: never cut short, which would fail open.
    # Sequentially consistent: the read after `forget_switch` must see the change
    # just written, and every process's read the newest change on any replica.
    with own_write_reads():
        rows = await get_client().query_all(
            "SELECT org FROM loom.org_model_calls FINAL WHERE enabled = 0"
        )
    return frozenset(str(r["org"]) for r in rows)


# The orgs switched off, as this process last read them.
_switch: PolledCopy[frozenset[str]] = PolledCopy("model-call switch", _read, log)


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
    """The org the current model call is for: `for_org`'s, else the
    request's (`genai.request_org`); "" when neither names one."""
    org = _org.get()
    return org if org not in _NO_ORG else genai.request_org()


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
        # Load refuses this pairing; a value set after load still never sends unmetered.
        if settings.inference_customer_required:
            raise Unattributed(
                "LOOM_INFERENCE_CUSTOMER_REQUIRED is set without LOOM_INFERENCE_CUSTOMER_HEADER"
            )
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


def names_one() -> bool:
    """Whether the current model call names a customer, as `require` checks,
    for a job that records a row naming none instead of calling. Logs nothing:
    the row is bad data, not a missed call path."""
    if not settings.inference_customer_header:
        return not settings.inference_customer_required
    return bool(current() or _system.get())


# The error a job records on a row that names no customer: retrying cannot
# help, since a row's org never changes, so the row is given up on at once.
NO_CUSTOMER = "MODEL_CALL_UNATTRIBUTED: the row names no org to bill"


def headers(org: str) -> dict[str, str]:
    """The customer and feature headers for one model request, for the org
    `require()` returned; {} when it returned none (no header configured)."""
    if not org:
        return {}
    # An org is named as the gateway knows it; the system customer as configured.
    named = settings.inference_customer_format.replace("{org}", org) if current() else org
    # Header values are latin-1 with no line breaks: quote whatever the org adds.
    out = {settings.inference_customer_header: quote(named, safe="-._~:@")}
    if settings.inference_feature_header:
        out[settings.inference_feature_header] = quote(gateway_feature(), safe="-._~:@")
    return out


def gateway_feature() -> str:
    """The feature as the gateway names it (`LOOM_INFERENCE_FEATURE_MAP_JSON`),
    else Loom's own name."""
    mine = feature()
    names = settings.feature_map()  # decoded once per value
    return names.get(mine) or names.get("*") or mine


def feature() -> str:
    """What made the call: the cascade stage when one is set (hyde,
    extract_typed, ...), else the op or worker (memory.search,
    worker.embedding)."""
    stage = genai.current_stage()
    if stage != "other":
        return stage
    return genai.current_operation() or "other"


async def by_org(orgs: Iterable[str]) -> dict[str, list[int]]:
    """The positions of `orgs` grouped by org, in first-seen order: one model
    call per group, since a call names a single customer. With no customer
    header nothing reads the customer, so every position is one group, unless
    an org among them is switched off: then each org is its own group, so the
    switch refuses that org's call and the others go on."""
    orgs = list(orgs)
    if not orgs:
        return {}
    if not settings.inference_customer_header and set(orgs).isdisjoint(await switched_off_orgs()):
        return {"": list(range(len(orgs)))}
    out: dict[str, list[int]] = {}
    for i, org in enumerate(orgs):
        out.setdefault(org, []).append(i)
    return out


async def switched_off(org: str) -> bool:
    """Whether `org`'s model calls are switched off. Raises SwitchUnread
    when this process's copy of the switch is too old to trust."""
    return org in await switched_off_orgs()


async def switched_off_orgs() -> frozenset[str]:
    """The orgs whose model calls are switched off, for a job to pass over;
    raises SwitchUnread when this process's copy is too old."""
    off = await _switch.get(
        refresh_s=settings.model_calls_policy_refresh_seconds,
        max_stale_s=settings.model_calls_policy_max_stale_seconds,
    )
    if off is None:
        raise SwitchUnread("the model-call switch could not be read; model calls wait until it is")
    return off


def forget_switch() -> None:
    """Make the next call read the switch again, for a change just made here."""
    _switch.forget()
