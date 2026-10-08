"""Secret redaction for the diagnostics the integration suite prints.

Failure reports carry request lines and server log tails; neither may carry a
bearer token, a JWT, a password or an OAuth code.
"""

from __future__ import annotations

import re

_JWT = re.compile(r"eyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]*")
_BEARER = re.compile(r"(?i)(bearer\s+)[^\s\"',]+")
_PAIR = re.compile(
    r"(?i)((?:password|passwd|secret|token|api_key|apikey|client_secret|code_verifier"
    r"|refresh_token|access_token|authorization)[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,}]+"
)
# An OAuth authorization code travels as a bare `code=` query parameter.
_QUERY_CODE = re.compile(r"([?&]code=)[^&\s]+")
MASK = "<redacted>"


def redact(text: str) -> str:
    """`text` with every credential-shaped value replaced by `MASK`."""
    text = _JWT.sub(MASK, text)
    text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
    text = _QUERY_CODE.sub(lambda m: m.group(1) + MASK, text)
    return _PAIR.sub(lambda m: m.group(1) + MASK, text)
