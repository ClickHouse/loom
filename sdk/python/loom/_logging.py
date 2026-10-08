"""Logging with one redaction point.

The SDK logs through the stdlib logger ``loom``: one line per completed call
at INFO (`memory.get 200 12 ms attempts=1 rid=…`), retries at WARNING, the
final failure at ERROR, request headers at DEBUG. The logger carries a
``NullHandler`` (the application decides where lines go) and a
``SensitiveHeadersFilter`` that rewrites any bearer token or credential
header value that reaches a record, so a header dict logged verbatim by
mistake still cannot leak the key. ``LOOM_LOG_LEVEL`` (a stdlib level name,
or off) sets the logger's level when the application has not; unset leaves
it to the root configuration.
"""

from __future__ import annotations

import logging
import os
import re
import warnings
from collections.abc import Mapping

logger = logging.getLogger("loom")

#: Credential headers: the scheme and the last four characters survive.
CREDENTIAL_HEADERS: frozenset[str] = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie"}
)

_BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic|Token)\s+([A-Za-z0-9._~+/=-]{8,})")


def redact_secret(secret: str) -> str:
    secret = secret.strip()
    return f"…{secret[-4:]}" if len(secret) > 8 else "…"


def redact_header_value(name: str, value: str) -> str:
    """The value of header `name` as it may appear in a log line."""
    if name.lower() not in CREDENTIAL_HEADERS:
        return value
    parts = value.strip().split(None, 1)
    if len(parts) == 2:
        return f"{parts[0]} {redact_secret(parts[1])}"
    return redact_secret(value)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """A copy of `headers` safe for a log line. The one redaction point."""
    return {k: redact_header_value(k, v) for k, v in headers.items()}


def redact_text(text: str) -> str:
    """Rewrite bearer / basic / token credentials embedded in free text."""
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} {redact_secret(m.group(2))}", text)


class SensitiveHeadersFilter(logging.Filter):
    """Rewrites credentials in a record's message and string arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_text(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                k: (redact_text(v) if isinstance(v, str) else v) for k, v in record.args.items()
            }
        return True


# Every stdlib level name (the server reads the same `LOOM_LOG_LEVEL`), and `off`.
_LEVELS = {
    "notset": logging.NOTSET,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
    "off": logging.CRITICAL + 10,
}


def parse_log_level(value: str, source: str) -> int:
    """A logging level from `value`, naming `source` when it is not one."""
    key = value.strip().lower()
    if key not in _LEVELS:
        raise ValueError(
            f"{source}: invalid log level {value!r}; expected one of {', '.join(_LEVELS)}"
        )
    return _LEVELS[key]


def configure_from_env(env: Mapping[str, str] | None = None) -> None:
    """Apply ``LOOM_LOG_LEVEL`` to the ``loom`` logger when it is set. It runs
    at import, so a value it cannot read is a warning, not an import error."""
    raw = (env if env is not None else os.environ).get("LOOM_LOG_LEVEL", "").strip()
    if not raw:
        return
    try:
        logger.setLevel(parse_log_level(raw, "LOOM_LOG_LEVEL"))
    except ValueError as e:
        warnings.warn(f"{e}; the level is left unchanged", RuntimeWarning, stacklevel=2)


if not any(isinstance(f, SensitiveHeadersFilter) for f in logger.filters):
    logger.addFilter(SensitiveHeadersFilter())
if not logger.handlers:
    logger.addHandler(logging.NullHandler())
configure_from_env()
