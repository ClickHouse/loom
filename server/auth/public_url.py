"""The base URL the server hands to clients.

The Setup page's config block, the `claude mcp add` line and a bot's
`claim_url` all carry the server's own address. Behind a TLS terminator the
server sees plain HTTP, so the address must come from the proxy's
`X-Forwarded-*` headers — and a proxy chain that overwrites
`X-Forwarded-Proto` with its own scheme (`http`) would make every config the
page produces name `http://…`. `LOOM_PUBLIC_BASE_URL` is the operator's fixed
answer when set;
otherwise the forwarded scheme and host, then the request's own.
"""

from __future__ import annotations

import re
from typing import Any

# A host header is echoed into URLs clients follow and paste; only a
# hostname or IP literal with an optional port is.
_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$"
)
_FALLBACK_HOST = "localhost:7777"


def public_base_url(request: Any, *, configured: str | None = None) -> str:
    """`https://loom.example` — no trailing slash, no path. A forwarded or
    Host value that is not a hostname[:port], or a scheme other than
    http/https, is ignored rather than echoed into a URL."""
    if configured is None:
        from server.settings import settings

        configured = str(settings.public_base_url)
    configured = configured.strip().rstrip("/")
    if configured:
        return configured
    headers = request.headers
    own_scheme = getattr(getattr(request, "url", None), "scheme", "http")
    # A comma-joined chain (proxy behind proxy) names the client-facing hop first.
    hosts = [
        str(h).split(",")[0].strip()
        for h in (headers.get("x-forwarded-host"), headers.get("host"))
        if h
    ]
    host = next((h for h in hosts if _HOST.fullmatch(h)), _FALLBACK_HOST)
    proto = str(headers.get("x-forwarded-proto") or own_scheme).split(",")[0].strip().lower()
    return f"{proto if proto in ('http', 'https') else 'http'}://{host}"
