"""Signed outbound webhooks, shared by the task inbox plane and memory webhooks.

Signed per the Standard Webhooks spec: ``webhook-id`` (the receiver's
idempotency key), ``webhook-timestamp`` (unix seconds, so a receiver can
refuse a replayed delivery) and ``webhook-signature: v1,<base64 HMAC-SHA256>``
over ``"{id}.{timestamp}.{body}"``. The secret is read from the environment
variable a registration names, and only a name of the registering org's own
(`webhook_secret_ref_ok`): it is never stored in ClickHouse and never logged.

A delivery goes to the address `vet_host` vetted, so a second DNS answer
cannot point it inward; the URL's name stays the Host header, the SNI and the
name the certificate is checked against. Redirects are not followed.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import logging
import os
import ssl
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from server.core.hosts import vet_host
from server.core.webhook_secrets import webhook_secret_ref_ok
from server.settings.catalog import ascii_host

log = logging.getLogger(__name__)


@functools.cache
def tls() -> ssl.SSLContext:
    """One TLS context for every delivery, built on the first: building one reads
    the CA bundle. Each delivery still has its own client and connections."""
    return httpx.create_ssl_context()


def read_secret(org: str, ref: str, env: Mapping[str, str] | None = None) -> bytes | None:
    """The signing key `ref` names in `env`, the process environment when None
    (surrounding whitespace, such as a trailing newline, dropped), or None when
    the name is not one of `org`'s own, the variable is unset, or its `whsec_`
    value is empty or not base64, or the value is not UTF-8: an empty key signs
    what anyone can sign.
    Each delivery worker says once which secret it cannot use."""
    if env is None:
        env = os.environ  # each secret is a variable a registration names, not a setting
    raw = env.get(ref, "").strip() if webhook_secret_ref_ok(org, ref) else ""
    if not raw:
        return None
    try:
        key = secret_bytes(raw)
    except ValueError:  # not base64, a non-ASCII `whsec_` value, or bytes that are not UTF-8
        key = b""
    return key or None


def secret_bytes(raw: str) -> bytes:
    """The HMAC key: a ``whsec_``-prefixed value is base64 (the spec's
    generated form), whitespace inside it, such as the line breaks of a wrapped
    value, dropped; anything else is the literal UTF-8 string."""
    if raw.startswith("whsec_"):
        encoded = "".join(raw[len("whsec_") :].split())
        return base64.b64decode(encoded, validate=True)
    return raw.encode("utf-8")


def sign(secret: bytes, *, msg_id: str, timestamp: int, body: bytes) -> str:
    """``v1,<base64 HMAC-SHA256("{id}.{timestamp}.{body}")>``."""
    signed = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def signed_headers(
    secret: bytes, *, msg_id: str, timestamp: int, body: bytes, user_agent: str
) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "user-agent": user_agent,
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign(secret, msg_id=msg_id, timestamp=timestamp, body=body),
    }


async def pinned_target(
    url: str,
    *,
    schemes: Collection[str] = ("http", "https"),
    private_hosts: Collection[str] = (),
    in_loop: bool = False,
) -> tuple[str, str, str, str]:
    """(refusal, request URL, Host header, TLS server name) for a webhook URL.

    The scheme must be one of `schemes`. The host must resolve to public
    addresses only (`vet_host`), and the request goes to the address vetted
    here. A host named in `private_hosts` (an operator's allowlist, spelled as
    `ascii_host` spells it) may resolve to a private or loopback address and is
    reached by its name. The name is resolved, matched and sent (Host, SNI) in its
    ASCII form: a Unicode one as its UTS 46 IDNA form. `in_loop` looks a public
    host up on the event loop (`vet_host`)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "the URL is not valid", "", "", ""
    if parts.scheme not in schemes or not host:
        return f"the URL must be {' or '.join(sorted(schemes))} with a host", "", "", ""
    try:
        port = parts.port
    except ValueError:
        return "the URL has an invalid port", "", "", ""
    try:  # the Host header and the SNI are ASCII
        host = ascii_host(host)
    except ValueError:  # IDNA cannot encode it
        host = ""
    if not host:
        return "the URL is not valid", "", "", ""
    refusal, address = await vet_host(
        host, allow_private=host in private_hosts, name_address=False, in_loop=in_loop
    )
    if refusal:
        return refusal, "", "", ""
    suffix = f":{port}" if port else ""
    name = f"[{host}]" if ":" in host else host
    if not address:  # an allowlisted host: connect by name
        return "", urlunsplit((parts.scheme, name + suffix, parts.path, parts.query, "")), "", ""
    literal = f"[{address}]" if ":" in address else address
    pinned = urlunsplit((parts.scheme, literal + suffix, parts.path, parts.query, ""))
    return "", pinned, name + suffix, host


@dataclass(frozen=True)
class Delivery:
    """One POST's outcome: the HTTP status (0 when none arrived) and, without
    one, why (the refusal or the transport error's type)."""

    status: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


async def post(
    transport: httpx.AsyncBaseTransport | None,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    schemes: Collection[str] = ("http", "https"),
    private_hosts: Collection[str] = (),
    ack_timeout_seconds: float,
) -> Delivery:
    """POST `body` to `url` with `headers`, on `transport` (None = the network).

    One deadline, `ack_timeout_seconds` (each caller's own), covers the whole
    delivery: the DNS lookup, the connection and the response's status line.
    The body of the answer is never read. A lookup cut by the deadline leaves
    its resolver thread to finish on its own (Python cannot stop it); the
    delivery does not wait for it. Each delivery has its own client: a pooled
    connection is keyed by the pinned address, so a second registration on the
    same address would reuse a TLS session verified for another name."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    try:
        async with asyncio.timeout(ack_timeout_seconds):
            refusal, pinned, host_header, server_name = await pinned_target(
                url, schemes=schemes, private_hosts=private_hosts
            )
            if refusal:
                log.warning("webhook: %s refused: %s", host, refusal)
                return Delivery(0, f"refused: {refusal}")
            extra_headers = {"Host": host_header} if host_header else {}
            extensions = {"sni_hostname": server_name} if server_name else {}
            async with (
                httpx.AsyncClient(
                    transport=transport, follow_redirects=False, verify=tls()
                ) as client,
                client.stream(
                    "POST",
                    pinned,
                    content=body,
                    headers={**headers, **extra_headers},
                    timeout=ack_timeout_seconds,
                    extensions=extensions,
                ) as response,
            ):
                return Delivery(int(response.status_code))
    except TimeoutError:
        log.info("webhook: no answer from %s within %ss", host, ack_timeout_seconds)
        return Delivery(0, "no answer: timeout")
    except httpx.HTTPError as e:
        log.info("webhook: no answer from %s: %s", host, type(e).__name__)
        return Delivery(0, f"no answer: {type(e).__name__}")
