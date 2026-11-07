"""The base URL the server hands to clients, and which hosts count as local.

The client setups a new token comes with, the `claude mcp add` line, a bot's
`claim_url` and the OAuth metadata all carry the server's own address.
`LOOM_PUBLIC_BASE_URL` is that address whenever it is set; a non-loopback
bind refuses to start without it. Otherwise (a loopback bind) the request's
own Host and scheme answer. `X-Forwarded-*` headers are never read here:
uvicorn applies X-Forwarded-Proto/For only from `LOOM_TRUSTED_PROXIES`, and
X-Forwarded-Host is ignored.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

# A host header is echoed into URLs clients follow and paste; only a
# hostname or IP literal with an optional port is.
_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$"
)
_FALLBACK_HOST = "localhost:7777"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def public_base_url(request: Any, *, configured: str | None = None) -> str:
    """`https://loom.example` — no trailing slash, no path. A Host value that
    is not a hostname[:port], or a scheme other than http/https, is ignored
    rather than echoed into a URL."""
    if configured is None:
        from server.settings import settings

        configured = str(settings.public_base_url)
    configured = configured.strip().rstrip("/")
    if configured:
        return configured
    host = str(request.headers.get("host") or "")
    if not _HOST.fullmatch(host):
        host = _FALLBACK_HOST
    scheme = str(getattr(getattr(request, "url", None), "scheme", "http")).lower()
    return f"{scheme if scheme in ('http', 'https') else 'http'}://{host}"


def is_loopback_host(host: str) -> bool:
    """True for localhost, 127.0.0.0/8 and ::1 (a bind address or hostname)."""
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_header_allowed(host_header: str, public_base: str) -> bool:
    """Whether a request's Host names this server: a loopback host or the
    hostname of `public_base`. Guards an auth-off server against DNS
    rebinding, where a page on another site reaches it under its own name."""
    host_header = host_header.strip()
    if not _HOST.fullmatch(host_header):
        return False
    hostname = urlsplit(f"//{host_header}").hostname
    if hostname and is_loopback_host(hostname):
        return True
    return bool(public_base) and hostname == urlsplit(public_base).hostname


def _origin_parts(url: str) -> tuple[str, str, int] | None:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    if not parts.hostname or port is None:
        return None
    return scheme, parts.hostname, port


def same_origin(origin: str, own_base: str) -> bool:
    """Whether a browser Origin is this server's own scheme, host and port."""
    mine = _origin_parts(own_base)
    return mine is not None and _origin_parts(origin) == mine


def origin_matches_host(origin: str, host_header: str) -> bool:
    """Whether a browser Origin names the host and port the request was sent to.
    Used when no public URL is configured: behind a TLS proxy whose
    X-Forwarded-Proto isn't trusted the request looks like http, so the scheme
    is not compared. A Host without a port matches the Origin's default port."""
    host_header = host_header.strip()
    theirs = _origin_parts(origin)
    if theirs is None or not _HOST.fullmatch(host_header):
        return False
    scheme, origin_host, origin_port = theirs
    parts = urlsplit(f"//{host_header}")
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return False
    return parts.hostname == origin_host and port == origin_port


def cors_origin_list(raw: str) -> list[str]:
    """LOOM_CORS_ORIGINS as a list. `*` is refused: CORS here allows
    credentials, so it would let every site make credentialed calls."""
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        raise RuntimeError(
            "refusing to start: LOOM_CORS_ORIGINS contains '*', and CORS is served with "
            "credentials allowed. List the exact origins instead."
        )
    return origins
