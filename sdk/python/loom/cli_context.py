"""Trusted CLI context shared by source controls and the server CLI."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx

from loom.errors import LoomError
from loom.ops import spec
from loom.retry import RetryPolicy
from loom.session_protection import (
    AutomaticCaptureSuppressed as AutomaticCaptureSuppressed,
)
from loom.session_protection import (
    SessionProtection,
    SessionProtectionUnavailable,
    observation_scope,
)
from loom.transport import Transport

CLI_CONTEXT_API = 1
CONTEXT_ENV = "LOOM_SOURCE_SESSION_CONTEXT"
T = TypeVar("T")


@dataclass
class _Intent:
    explicit: bool
    capsule: str | None
    active: bool = True
    task: object | None = None


_intent: contextvars.ContextVar[_Intent | None] = contextvars.ContextVar(
    "loom_sdk_cli_intent", default=None
)


@contextlib.contextmanager
def command_intent(args: Any) -> Iterator[None]:
    """Consume explicit intent once, without retaining it on parsed args or clients."""
    value = _Intent(
        vars(args).pop("request_intent", "automatic") == "explicit",
        os.environ.get(CONTEXT_ENV),
    )
    token = _intent.set(value)
    try:
        yield
    finally:
        value.active = False
        _intent.reset(token)


async def guarded_command(args: Any, action: Callable[[Any], Awaitable[T]]) -> T:
    method, path = "POST", "/unknown-cli-route"
    if args.cmd == "call":
        path = args.target if args.target.startswith("/") else "/v1/" + args.target
        method = args.method or (
            "POST" if args.body is not None or not args.target.startswith("/") else "GET"
        )
    intent = _intent.get()
    if intent is not None and intent.capsule != os.environ.get(CONTEXT_ENV):
        raise ContextUnavailable("Trusted CLI session environment changed during dispatch.")
    task = asyncio.current_task()
    if intent is not None and intent.task is None:
        intent.task = task
    async with configured_scope(
        method,
        path,
        explicit=intent is not None and intent.active and intent.explicit and intent.task is task,
    ):
        return await action(args)


@contextlib.asynccontextmanager
async def configured_scope(
    method: str, path: str, *, explicit: bool = False
) -> AsyncIterator[None]:
    """Bind the SDK CLI's maintained clients to the inherited application context."""
    capsule = os.environ.get(CONTEXT_ENV)
    if capsule is None:
        yield
        return
    family = open_context(capsule)
    with observation_scope((family,)), request_scope(family, method, path, explicit=explicit):
        yield


class ContextUnavailable(RuntimeError):
    """Configured application authority cannot be restored."""


def session_environment(path: str) -> dict[str, str]:
    """Provision once in the trusted launcher; retain this environment on resume."""
    from loom.source_cli import open_session

    family = open_session(path)
    return {
        CONTEXT_ENV: json.dumps(
            {"version": 1, "path": path, "pointer": family.pointer()},
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def open_context(capsule: str, *, session_file: str | None = None) -> SessionProtection:
    """Reopen the pinned pointer, without creating or repairing authority."""
    from loom.source_cli import _json_object, open_session

    try:
        if len(capsule.encode()) > 16 * 1024:
            raise ValueError
        value = _json_object(capsule)
        if (
            set(value) != {"version", "path", "pointer"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or not isinstance(value["path"], str)
            or not isinstance(value["pointer"], dict)
            or (session_file is not None and value["path"] != session_file)
        ):
            raise ValueError
        family = open_session(value["path"])
        if family.pointer() != value["pointer"]:
            raise ValueError
        return family
    except (OSError, ValueError, SessionProtectionUnavailable) as exc:
        raise ContextUnavailable(
            "Trusted CLI session context is unavailable or replaced; restore the original "
            "pointer and proof. No reset or cleanup was attempted."
        ) from exc


@contextlib.contextmanager
def request_scope(
    family: SessionProtection, method: str, path: str, *, explicit: bool = False
) -> Iterator[None]:
    """Only generated read metadata or direct user intent permits a protected call."""
    family.require_ready()
    route = urlsplit(path).path
    operation = spec(route[4:]) if method == "POST" and route.startswith("/v1/") else None
    readonly = operation is not None and operation.read_only
    scope = contextlib.nullcontext() if explicit or readonly else family.automatic()
    with scope:
        yield


def _target(url: str) -> tuple[str, str]:
    value = urlsplit(url)
    if (
        value.scheme not in ("http", "https")
        or not value.netloc
        or value.username is not None
        or value.password is not None
        or value.fragment
    ):
        raise ContextUnavailable(
            "Trusted CLI requests require an HTTP server URL without userinfo."
        )
    return value.scheme + "://" + value.netloc, (value.path or "/") + (
        "?" + value.query if value.query else ""
    )


def _transport(base: str, token: str, family: SessionProtection, timeout: float) -> Transport:
    return Transport(
        base,
        api_key=token,
        session_protection=family,
        timeout_s=timeout,
        retry=RetryPolicy(max_attempts=1),
        pacing=False,
        ratelimit_max_wait_s=0,
    )


async def request(
    capsule: str,
    method: str,
    url: str,
    token: str,
    *,
    body: Mapping[str, Any] | None = None,
    content: bytes | None = None,
    timeout_s: float = 90.0,
    explicit: bool = False,
) -> httpx.Response:
    """One maintained SDK dispatch, observed before returning any response."""
    family = open_context(capsule)
    base, path = _target(url)
    with request_scope(family, method, path, explicit=explicit):
        transport = _transport(base, token, family, timeout_s)
        try:
            try:
                return await transport.request(method, path, json=body, content=content)
            except LoomError as exc:
                # The SDK already observed the error; never copy private diagnostics to stderr.
                return httpx.Response(
                    exc.status or 503,
                    json={"detail": {"code": "CLI_REQUEST_FAILED"}},
                    request=httpx.Request(method, url),
                )
        finally:
            await transport.close()


@contextlib.asynccontextmanager
async def stream(
    capsule: str,
    url: str,
    token: str,
    *,
    body: Mapping[str, Any],
    timeout_s: float = 90.0,
    explicit: bool = False,
) -> AsyncIterator[httpx.Response]:
    """Retain SDK observation through stream consumption, cancellation and close."""
    family = open_context(capsule)
    base, path = _target(url)
    with request_scope(family, "POST", path, explicit=explicit):
        transport = _transport(base, token, family, timeout_s)
        try:
            async with transport.stream(path, json=body) as response:
                yield response
        finally:
            await transport.close()


def run_sync(factory: Callable[[], Awaitable[T]]) -> T:
    """Run a fresh async client even when a synchronous CLI is called within a loop."""

    async def direct() -> T:
        return await factory()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(direct())

    result: Future[T] = Future()
    context = contextvars.copy_context()
    running: list[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = []
    cancelled = threading.Event()

    async def invoke() -> T:
        task = asyncio.current_task()
        assert task is not None
        running.append((asyncio.get_running_loop(), task))
        if cancelled.is_set():
            task.cancel()
        return await factory()

    def worker() -> None:
        try:
            result.set_result(context.run(asyncio.run, invoke()))
        except BaseException as exc:  # noqa: BLE001 — transfer every failure to the waiting caller
            result.set_exception(exc)

    thread = threading.Thread(target=worker, name="loom-cli-request")
    thread.start()
    try:
        return result.result()
    except KeyboardInterrupt:
        cancelled.set()
        if running:
            loop, task = running[0]
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        raise
    finally:
        thread.join()
