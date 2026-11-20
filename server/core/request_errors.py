"""Classify private HTTP failures after the request owner has unwound."""

from __future__ import annotations

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from starlette.requests import ClientDisconnect

_MARK_KEY = "_loom_private_request_error"
_MARK_TOKEN = object()


class MarkedRequestError(Exception):
    """Constant application failure; its original context is never a public log."""

    def __init__(self) -> None:
        super().__init__("Private request failed")


def preserve_error(error: BaseException) -> bool:
    """Keep control-flow objects and exception groups intact."""
    return not isinstance(error, Exception) or isinstance(
        error,
        (
            BaseExceptionGroup,
            ClientDisconnect,
            OSError,
            EndOfStream,
            BrokenResourceError,
            ClosedResourceError,
        ),
    )


def mark_preserved_error(error: BaseException) -> None:
    """Bind an opaque classification without invoking an exception's overrides."""
    if not marked_error(error):
        BaseException.__setattr__(error, _MARK_KEY, _MARK_TOKEN)


def marked_error(error: BaseException | None) -> bool:
    """Read server classification without formatting the error or its chains."""
    if error is None:
        return False
    if isinstance(error, MarkedRequestError):
        return True
    try:
        return BaseException.__getattribute__(error, _MARK_KEY) is _MARK_TOKEN
    except AttributeError:
        return False
