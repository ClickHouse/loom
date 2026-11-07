"""What a namespace's `classify_endpoint` may be.

memory.set with `classify=True` posts the memory's value there. While the
gateway is required the endpoint must be an https URL with no explicit port at
one of LOOM_INFERENCE_GATEWAY_HOSTS: a namespace admin can neither send content
elsewhere nor reach another port or service of a host to probe it. A URL at a
listed gateway host is held to the same rule when the gateway is not required:
another port of the gateway's host is still inward reach. Otherwise it must be
https (or a host LOOM_MEMORY_CLASSIFY_PRIVATE_HOSTS names), and the call reaches
only a public address it vetted (`server.memory.app.classify`), so a namespace
admin cannot aim the server at its own network. No endpoint carries a user name or
password, and every check reads one host: httpx's and `urlsplit`'s must agree.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator

from server.settings import settings
from server.settings.catalog import ascii_host, url_host


def via_gateway(url: str) -> bool:
    """Whether a classifier at `url` is called as a model call, through the gateway:
    while the gateway is required, or at one of its listed hosts. Read only for a URL
    `classify_refusal` passes, so httpx's host and the call's are one."""
    return settings.gateway_required() or url_host(url.strip()) in settings.gateway_hosts()


def _one_host(url: str) -> tuple[str, str]:
    """(refusal, host): the one host httpx (the gateway check, the model call) and
    `urlsplit` (the off-gateway call, `webhooks.pinned_target`) both read in `url`,
    as `ascii_host` spells it. Refused when they would read two, or either none."""
    # urlsplit drops a tab or a newline that httpx refuses, and either may read a
    # control character apart: no URL carries one.
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        return "it may not carry a control or whitespace character", ""
    try:
        parts = urlsplit(url)
        host = ascii_host(parts.hostname or "")
        parts.port  # noqa: B018 — an invalid port raises here, as at the call
    except ValueError:  # an IDNA error is a ValueError too
        return "it is not a valid URL", ""
    if not host:
        return "it names no host", ""
    if host != url_host(url):  # "" when httpx cannot parse it
        return "it is not a valid URL", ""
    return "", host


def classify_refusal(url: str) -> str:
    """Why a classifier at `url` is refused; "" when it may be called (a host off the
    gateway's network is still vetted at the call)."""
    url = url.strip()
    if not url:
        return ""
    refusal, host = _one_host(url)
    if refusal:
        return refusal
    parts = urlsplit(url)
    # The call drops them, and namespace.get would hand the credential back.
    if parts.username is not None or parts.password is not None:
        return "the URL may not carry a user name or password"
    if not via_gateway(url):
        private = host in settings.memory_classify_private_host_set()
        if parts.scheme != "https" and not (private and parts.scheme == "http"):
            return "it must be https"
        return ""
    if settings.gateway_required() and host not in settings.gateway_hosts():
        return f"{host} is not in LOOM_INFERENCE_GATEWAY_HOSTS"
    if parts.scheme != "https" or parts.port is not None:
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
    return value.strip() if value else value  # stored as the call reads it


ClassifyEndpoint = Annotated[str | None, AfterValidator(check_classify_endpoint)]
