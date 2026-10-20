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
import binascii
import hashlib
import hmac
import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from server.core.hosts import vet_host

log = logging.getLogger(__name__)

# A registration's secret ref must name an environment variable with this
# prefix: any other name could hand a server secret to a URL the caller chose
# as the signing key.
WEBHOOK_SECRET_PREFIX = "LOOM_WEBHOOK_SECRET_"
# How long a receiver has to answer before the delivery counts as failed.
ACK_TIMEOUT_SECONDS = 10.0
# One TLS context for every delivery: building one reads the CA bundle. Each
# delivery still has its own client and so its own connections.
_TLS = httpx.create_ssl_context()


def webhook_secret_org_prefix(org: str) -> str:
    """The start of every webhook secret name `org` may use:
    `LOOM_WEBHOOK_SECRET_<ORG>__`, ORG the org id with a-z upper-cased, digits
    kept and every other byte (A-Z too) as `_` and two hex digits, so no two
    orgs share it (`acme-x` → `ACME_2DX`, `acme_x` → `ACME_5FX`). Without it
    any tenant could name another tenant's secret and have Loom sign with it."""
    encoded = "".join(
        chr(b).upper() if chr(b) in "abcdefghijklmnopqrstuvwxyz0123456789" else f"_{b:02X}"
        for b in org.encode()
    )
    return f"{WEBHOOK_SECRET_PREFIX}{encoded}__"


def webhook_secret_ref_ok(org: str, ref: str) -> bool:
    """Whether `ref` names one of `org`'s own webhook secrets: the org prefix,
    then letters and digits only, so no other org's prefix can match it."""
    name = ref.removeprefix(webhook_secret_org_prefix(org))
    return name != ref and bool(re.fullmatch(r"[A-Za-z0-9]+", name))


# Secret refs already reported as not usable: said once each, not on every pass.
_warned_unusable: set[str] = set()


def read_secret(org: str, ref: str, env: Mapping[str, str], *, warn: bool = True) -> bytes | None:
    """The signing key `ref` names in `env` (surrounding whitespace, such as a
    trailing newline, dropped), or None when the name is not one of `org`'s own,
    the variable is unset, or its `whsec_` value is not base64. `warn=False`
    for a caller that says so itself."""
    raw = env.get(ref, "").strip() if webhook_secret_ref_ok(org, ref) else ""
    if not raw:
        return None
    try:
        return secret_bytes(raw)
    except binascii.Error:
        if warn and ref not in _warned_unusable:
            _warned_unusable.add(ref)
            log.warning("webhook: the secret in %s is not usable: whsec_ must be base64", ref)
        return None


def secret_bytes(raw: str) -> bytes:
    """The HMAC key: a ``whsec_``-prefixed value is base64 (the spec's
    generated form); anything else is the literal UTF-8 string."""
    if raw.startswith("whsec_"):
        return base64.b64decode(raw[len("whsec_") :], validate=True)
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
) -> tuple[str, str, str, str]:
    """(refusal, request URL, Host header, TLS server name) for a webhook URL.

    The scheme must be one of `schemes`. The host must resolve to public
    addresses only (`vet_host`), and the request goes to the address vetted
    here. A host named in `private_hosts` (an operator's allowlist, lower case)
    may resolve to a private or loopback address and is reached by its name."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in schemes or not host:
        return f"the URL must be {' or '.join(sorted(schemes))} with a host", "", "", ""
    try:
        port = parts.port
    except ValueError:
        return "the URL has an invalid port", "", "", ""
    refusal, address = await vet_host(
        host, allow_private=host.lower() in private_hosts, name_address=False
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
) -> Delivery:
    """POST `body` to `url` with `headers`, on `transport` (None = the network).

    One deadline, ACK_TIMEOUT_SECONDS, covers the whole delivery: the DNS lookup,
    the connection and the response's status line. The body of the answer is
    never read. A lookup cut by the deadline leaves its resolver thread to finish
    on its own (Python cannot stop it); the delivery does not wait for it. Each
    delivery has its own client: a pooled connection is keyed by the pinned
    address, so a second registration on the same address would reuse a TLS
    session verified for another name."""
    host = urlsplit(url).hostname or ""
    try:
        async with asyncio.timeout(ACK_TIMEOUT_SECONDS):
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
                    transport=transport, follow_redirects=False, verify=_TLS
                ) as client,
                client.stream(
                    "POST",
                    pinned,
                    content=body,
                    headers={**headers, **extra_headers},
                    timeout=ACK_TIMEOUT_SECONDS,
                    follow_redirects=False,
                    extensions=extensions,
                ) as response,
            ):
                return Delivery(int(response.status_code))
    except TimeoutError:
        log.info("webhook: no answer from %s within %ss", host, ACK_TIMEOUT_SECONDS)
        return Delivery(0, "no answer: timeout")
    except httpx.HTTPError as e:
        log.info("webhook: no answer from %s: %s", host, type(e).__name__)
        return Delivery(0, f"no answer: {type(e).__name__}")
