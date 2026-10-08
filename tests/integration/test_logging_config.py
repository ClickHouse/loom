"""Structured logging — JSON formatter + request_id ContextVar."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _emit_to_buffer(formatter, level=logging.INFO, msg="hello", extras=None) -> str:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extras:
        for k, v in extras.items():
            setattr(record, k, v)
    return formatter.format(record)


def test_json_formatter_emits_parseable_json() -> None:
    from server.core.logging_config import _JsonFormatter

    out = _emit_to_buffer(_JsonFormatter(), msg="kid rotation complete")
    payload = json.loads(out)
    assert payload["msg"] == "kid rotation complete"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert "ts" in payload


def test_json_formatter_includes_request_id_when_set() -> None:
    from server.core.logging_config import _JsonFormatter, request_id_var

    token = request_id_var.set("req-abc-123")
    try:
        out = _emit_to_buffer(_JsonFormatter())
    finally:
        request_id_var.reset(token)
    payload = json.loads(out)
    assert payload["request_id"] == "req-abc-123"


def test_json_formatter_surfaces_extras() -> None:
    from server.core.logging_config import _JsonFormatter

    out = _emit_to_buffer(
        _JsonFormatter(),
        extras={
            "op": "memory.set",
            "duration_ms": 42,
        },
    )
    payload = json.loads(out)
    assert payload["op"] == "memory.set"
    assert payload["duration_ms"] == 42


def test_text_formatter_prefixes_request_id() -> None:
    from server.core.logging_config import _TextFormatter, request_id_var

    token = request_id_var.set("rid-xyz")
    try:
        out = _emit_to_buffer(_TextFormatter("%(message)s"))
    finally:
        request_id_var.reset(token)
    assert "[req=rid-xyz]" in out


def test_json_formatter_handles_unserializable_extras() -> None:
    """A non-JSON-serializable extra (like a Path) gets repr'd, not dropped."""
    from server.core.logging_config import _JsonFormatter

    out = _emit_to_buffer(_JsonFormatter(), extras={"path_obj": Path("/tmp")})
    payload = json.loads(out)
    assert "path_obj" in payload  # didn't crash; value coerced
