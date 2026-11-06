"""What a namespace's `classify_endpoint` may be.

memory.set with `classify=True` posts the memory's value there. While the
gateway is required the endpoint must be an https URL with no explicit port at
one of LOOM_INFERENCE_GATEWAY_HOSTS: a namespace admin can neither send content
elsewhere nor reach another port or service of a host to probe it. Otherwise it
must be https (or a host LOOM_MEMORY_CLASSIFY_PRIVATE_HOSTS names), and the call
reaches only a public address it vetted (`server.memory.app.classify`), so a
namespace admin cannot aim the server at its own network.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

import httpx
from pydantic import AfterValidator

from server.settings import settings


def classify_refusal(url: str) -> str:
    """Why a classifier at `url` is refused; "" when it may be called (a host off the
    gateway's network is still vetted at the call)."""
    if not url.strip():
        return ""
    if not settings.gateway_required():
        # Parsed as the call parses it (`webhooks.pinned_target`), so both read one host.
        try:
            parts = urlsplit(url.strip())
            host = (parts.hostname or "").lower()
        except ValueError:
            return "it is not a valid URL"
        private = host in settings.memory_classify_private_host_set()
        if parts.scheme != "https" and not (private and parts.scheme == "http"):
            return "it must be https"
        return ""
    host = settings.off_gateway(url)
    if host:
        return f"{host} is not in LOOM_INFERENCE_GATEWAY_HOSTS"
    parsed = httpx.URL(url.strip())  # off_gateway parsed it already
    if parsed.scheme != "https" or parsed.port is not None:
        return "it must be https at the default port"
    return ""


def check_classify_endpoint(value: str | None) -> str | None:
    """The endpoint, or ValueError (a 422) when it is refused."""
    refusal = classify_refusal(value) if value else ""
    if refusal:
        why = (
            f", while {settings.gateway_required_by()} sends memory content only to the gateway"
            if settings.gateway_required()
            else ""
        )
        raise ValueError(f"classify_endpoint: {refusal}{why}")
    return value


ClassifyEndpoint = Annotated[str | None, AfterValidator(check_classify_endpoint)]
