"""Lazy installed-SDK bridge for application-owned CLI session context."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast

CONTEXT_ENV = "LOOM_SOURCE_SESSION_CONTEXT"
_DEPENDENCY = (
    "Trusted CLI session context requires the matching-release clickhouse-loom Python SDK "
    "alongside the server CLI. Install the SDK from the same release artifact. "
    "The server-only wheel cannot provide protected requests."
)


class CLIContextError(RuntimeError):
    """A configured context refused before an unprotected request could be sent."""


def _task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@dataclass
class _Invocation:
    capsule: str | None
    explicit: bool
    thread: int
    task: object | None
    active: bool = True
    parent: _Invocation | None = None


_current: contextvars.ContextVar[_Invocation | None] = contextvars.ContextVar(
    "loom_cli_invocation", default=None
)


@contextlib.contextmanager
def invocation(intent: str = "automatic") -> Iterator[None]:
    """Bind one dispatch; expire authority in contexts copied by retained callbacks."""
    value = _Invocation(
        os.environ.get(CONTEXT_ENV), intent == "explicit", threading.get_ident(), _task()
    )
    token = _current.set(value)
    try:
        yield
    finally:
        value.active = False
        _current.reset(token)


def capsule() -> str | None:
    value = _current.get()
    raw = os.environ.get(CONTEXT_ENV)
    if value is not None and value.capsule != raw:
        raise CLIContextError(
            "Trusted CLI session environment changed during dispatch; restore it."
        )
    return raw


def configured() -> bool:
    return capsule() is not None


def _explicit() -> bool:
    value = _current.get()
    if value is None or not value.active or not value.explicit:
        return False
    parent = value.parent
    while parent is not None:
        if not parent.active:
            return False
        parent = parent.parent
    if value.thread != threading.get_ident():
        return False
    task = _task()
    if value.task is None and task is not None:
        value.task = task
    return value.task is task


def _entry() -> Any:
    try:
        entry = importlib.import_module("loom.cli_context")
    except ImportError:
        raise CLIContextError(_DEPENDENCY) from None
    if getattr(entry, "CLI_CONTEXT_API", None) != 1:
        raise CLIContextError(_DEPENDENCY)
    return entry


def require_sdk() -> None:
    """Refuse delegation to an SDK without the inherited context contract."""
    _entry()


def run_async[T](factory: Callable[[], Awaitable[T]]) -> T:
    """Keep a configured CLI dispatch and its resources on one owned async loop."""
    if not configured():
        try:
            asyncio.get_running_loop()
        except RuntimeError:

            async def ordinary() -> T:
                return await factory()

            return asyncio.run(ordinary())
        raise CLIContextError("Call the synchronous CLI outside the active event loop.")
    entry = _entry()
    parent = _current.get()
    raw, explicit = capsule(), _explicit()

    async def owned() -> T:
        child = _Invocation(raw, explicit, threading.get_ident(), _task(), parent=parent)
        token = _current.set(child)
        try:
            return await factory()
        finally:
            child.active = False
            _current.reset(token)

    return cast(T, entry.run_sync(owned))


async def post(
    client: Any, url: str, token: str, *, body: dict[str, Any], timeout_s: float = 90.0
) -> Any:
    """Keep ordinary clients unchanged; configured clients use observed SDK dispatch."""
    raw = capsule()
    if raw is None:
        return await client.post(url, json=body)
    entry = _entry()
    try:
        return await entry.request(
            raw, "POST", url, token, body=body, timeout_s=timeout_s, explicit=_explicit()
        )
    except (
        RuntimeError,
        OSError,
        ValueError,
        entry.AutomaticCaptureSuppressed,
        entry.SessionProtectionUnavailable,
    ) as exc:
        raise CLIContextError(
            "Trusted CLI session protection refused; retain the original context and proof."
        ) from exc


def send(req: Any, timeout: float) -> tuple[int, dict[str, Any]] | None:
    """urllib-compatible JSON/file boundary; None selects the ordinary unbound path."""
    raw = capsule()
    if raw is None:
        return None
    entry = _entry()
    explicit = _explicit()
    content = req.data
    body = None
    if content is not None and req.get_header("Content-type", "").startswith("application/json"):
        import json

        body = json.loads(content)
        content = None
    auth = req.get_header("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    try:
        response = entry.run_sync(
            lambda: entry.request(
                raw,
                req.get_method(),
                req.full_url,
                token,
                body=body,
                content=content,
                timeout_s=timeout,
                explicit=explicit,
            )
        )
        result = response.json()
        return response.status_code, result if isinstance(result, dict) else {}
    except (
        RuntimeError,
        OSError,
        ValueError,
        entry.AutomaticCaptureSuppressed,
        entry.SessionProtectionUnavailable,
    ) as exc:
        raise CLIContextError(
            "Trusted CLI session protection refused; retain the original context and proof."
        ) from exc
