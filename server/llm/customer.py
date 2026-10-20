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
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import time
from collections.abc import Iterable, Iterator
from urllib.parse import quote

from server.core.errors import LoomServerError
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


def require() -> str:
    """The customer the current model call is for, checked before anything is
    sent, cached or recorded: "" with no customer header configured. Raises
    Unattributed, and logs it, when the call names none."""
    if not settings.inference_customer_header:
        return ""
    org = current()
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


def headers() -> dict[str, str]:
    """The customer and feature headers for one model request; {} with no
    customer header configured. Raises Unattributed when the call names no
    customer."""
    org = require()
    if not org:
        return {}
    # Header values are latin-1 with no line breaks; an org id never needs more.
    named = settings.inference_customer_format.replace("{org}", org)
    out = {settings.inference_customer_header: quote(named, safe="-._~:@")}
    if settings.inference_feature_header:
        out[settings.inference_feature_header] = quote(gateway_feature(), safe="-._~:")
    return out


def gateway_feature() -> str:
    """The feature as the gateway names it (`LOOM_INFERENCE_FEATURE_MAP_JSON`),
    else Loom's own name."""
    mine = feature()
    names = _feature_map(settings.inference_feature_map_json)
    return names.get(mine) or names.get("*") or mine


@functools.lru_cache(maxsize=4)
def _feature_map(raw: str) -> dict[str, str]:
    # Parsed once per value, not on every model call; settings validated it at load.
    return settings.feature_map()


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
