"""Scoped restrictions on diagnostics derived from private source work."""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry.instrumentation.utils import suppress_instrumentation

_codes_only: ContextVar[bool] = ContextVar("loom_codes_only_diagnostics", default=False)


def codes_only() -> bool:
    return _codes_only.get()


def public_diagnostics() -> bool:
    """Allow a sink only while both the logical scope and HTTP owner are ordinary."""
    try:
        from server.core.request_work import http_mirror_marked

        return not codes_only() and not http_mirror_marked()
    except Exception:  # noqa: BLE001 — an unavailable privacy decision drops diagnostics
        return False


@contextmanager
def private_work() -> Iterator[None]:
    for handler in (*logging.getLogger().handlers, logging.lastResort):
        if handler is not None and public_log not in handler.filters:
            handler.addFilter(public_log)
    token = _codes_only.set(True)
    try:
        with suppress_instrumentation():
            yield
    finally:
        _codes_only.reset(token)


def public_only[**P](function: Callable[P, None]) -> Callable[P, None]:
    """Drop a metric call derived from private work."""

    @functools.wraps(function)
    def emit(*args: P.args, **kwargs: P.kwargs) -> None:
        if public_diagnostics():
            function(*args, **kwargs)

    return emit


def public_log(record: logging.LogRecord) -> bool:
    """Handler filter: descendants propagate to handlers without root logger filters."""
    try:
        from server.core.request_errors import marked_error

        if not public_diagnostics() or marked_error(sys.exception()):
            return False
        if record.exc_info:
            if not isinstance(record.exc_info, tuple) or len(record.exc_info) != 3:
                return False
            if marked_error(record.exc_info[1]):
                return False
        return True
    except Exception:  # noqa: BLE001 — classification failure drops the record before formatting
        return False
