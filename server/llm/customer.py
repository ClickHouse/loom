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
from collections.abc import Iterator
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


def headers() -> dict[str, str]:
    """The customer and feature headers for one model request; {} with no
    customer header configured. Raises Unattributed when the call names no
    customer."""
    name = settings.inference_customer_header
    if not name:
        return {}
    org = current()
    if not org:
        if not _system.get():
            raise Unattributed(
                f"a model call ({feature()}) names no customer: run it for an org "
                "(server.llm.customer.for_org) or as the deployment's (customer.system)"
            )
        org = settings.inference_system_customer
    # Header values are latin-1 with no line breaks; an org id never needs more.
    out = {name: quote(org, safe="-._~:@")}
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
