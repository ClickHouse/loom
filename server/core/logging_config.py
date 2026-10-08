"""Structured logging configuration.

`LOOM_LOG_FORMAT=json` (production): one line per record as JSON with
`ts, level, logger, msg, request_id?`. Pipes cleanly into HyperDX's OTLP
collector or any JSON-aware log shipper.

`LOOM_LOG_FORMAT=text` (default, dev-friendly): human-readable.

Use:
    from server.core.logging_config import configure_logging
    configure_logging()   # idempotent; called once at app startup
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# Context var the transports set per request so log lines carry
# request_id without callers having to thread it through. Async-safe.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            # Not `formatTime`: it renders through `time.strftime`, which has
            # no `%f`, in local time.
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Surface any extras attached via logger.info("...", extra={...})
        for k, v in record.__dict__.items():
            if k in payload:
                continue
            if k.startswith("_") or k in {
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "message",
                "module",
                "msecs",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
                "taskName",
            }:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    """Dev-friendly: timestamp + level + logger + msg + (request_id)."""

    DEFAULT_FMT = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        rid = request_id_var.get()
        if rid:
            base = f"[req={rid}] {base}"
        return base


_configured = False


def configure_logging() -> None:
    """Install JSON or text formatter based on settings. Idempotent."""
    global _configured
    if _configured:
        return
    from server.settings import settings

    fmt = settings.log_format
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_TextFormatter(_TextFormatter.DEFAULT_FMT))

    root = logging.getLogger()
    # Replace any existing handlers so uvicorn's default doesn't double-print
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet the noisiest defaults; tune in production as needed
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)

    _configured = True


__all__ = ["configure_logging", "request_id_var"]
