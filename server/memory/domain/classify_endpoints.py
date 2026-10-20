"""What a namespace's `classify_endpoint` may be.

memory.set with `classify=True` posts the memory's value there, so while the
gateway is required the endpoint must be an https URL with no explicit port at
one of LOOM_INFERENCE_GATEWAY_HOSTS: a namespace admin can neither send content
elsewhere nor reach another port or service of a host to probe it.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from pydantic import AfterValidator

from server.settings import settings


def classify_refusal(url: str) -> str:
    """Why the gateway refuses a classifier at `url`; "" when it may be called."""
    if not url.strip() or not settings.gateway_required():
        return ""
    host = settings.off_gateway(url)
    if host:
        return f"{host} is not in LOOM_INFERENCE_GATEWAY_HOSTS"
    parsed = httpx.URL(url.strip())  # off_gateway parsed it already
    if parsed.scheme != "https" or parsed.port is not None:
        return "it must be https at the default port"
    return ""


def check_classify_endpoint(value: str | None) -> str | None:
    """The endpoint, or ValueError (a 422) when the gateway refuses it."""
    refusal = classify_refusal(value) if value else ""
    if refusal:
        raise ValueError(
            f"classify_endpoint: {refusal}, while {settings.gateway_required_by()} "
            "sends memory content only to the gateway"
        )
    return value


ClassifyEndpoint = Annotated[str | None, AfterValidator(check_classify_endpoint)]
