"""Request-owned work that settles before publication or the next marked call."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import Context, ContextVar, copy_context
from typing import Any

import anyio

from server.core.ch import class_timeout_s, current_query_tag, query_tag, statement_timeout

log = logging.getLogger("loom.request_tail")
# not a setting: one required publication attempt has a fixed cleanup safety ceiling.
REQUIRED_STEP_TIMEOUT_S = 60.0


class RequiredTailError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Required request work was not confirmed")


_Publication = tuple[object, Context, Callable[[], Awaitable[Any]], Callable[[], bool]]
_publication_group: ContextVar[dict[int, _Publication] | None] = ContextVar(
    "loom_publication_group", default=None
)


class _Tail:
    __slots__ = (
        "closed",
        "completed",
        "has_required",
        "keys",
        "lock",
        "required_failed",
        "publications",
        "steps",
    )

    def __init__(self) -> None:
        self.steps: list[tuple[str, Callable[[], Awaitable[Any]], bool]] = []
        self.keys: set[str] = set()
        self.closed = False
        self.completed = False
        self.has_required = False
        self.required_failed = False
        self.lock = asyncio.Lock()
        self.publications: dict[int, _Publication] = {}


_tail: ContextVar[_Tail | None] = ContextVar("loom_request_tail", default=None)


def active() -> bool:
    tail = _tail.get()
    return tail is not None and not tail.closed


def required_active() -> bool:
    """Whether this request can still own required private-work delivery."""
    tail = _tail.get()
    return tail is not None and not tail.closed and not tail.required_failed


def fail_required() -> bool:
    """Retain a failed mandatory capture without running or closing request work."""
    tail = _tail.get()
    if tail is None:
        return False
    tail.has_required = True
    tail.required_failed = True
    return True


def defer(step: Callable[[], Awaitable[Any]]) -> bool:
    """Run ordinary best-effort work before the response starts."""
    tail = _tail.get()
    if tail is None or tail.closed:
        return False
    tail.steps.append((current_query_tag(), step, False))
    return True


def defer_once(key: str, step: Callable[[], Awaitable[Any]]) -> bool:
    """Defer ordinary work once per key while registration remains open."""
    tail = _tail.get()
    if tail is None or tail.closed:
        return False
    if key not in tail.keys:
        tail.keys.add(key)
        tail.steps.append((current_query_tag(), step, False))
    return True


def deferred(key: str) -> bool:
    tail = _tail.get()
    return tail is not None and key in tail.keys


def defer_required(step: Callable[[], Awaitable[Any]]) -> bool:
    """Register one owned required attempt; its logical caller deduplicates registration."""
    tail = _tail.get()
    if tail is None or tail.closed or tail.required_failed:
        return False
    tail.has_required = True
    tail.steps.append((current_query_tag(), step, True))
    return True


class _PublicationGroup:
    def __init__(self, tail: _Tail | None, parent: dict[int, _Publication] | None) -> None:
        self.tail, self.parent = tail, parent
        self.entries: dict[int, _Publication] = {}

    def commit(self) -> None:
        """Keep a branch's selected frame; abandoned branches retain no guards."""
        if self.tail is None:
            return
        if self.tail.closed or self.parent is None:
            raise RequiredTailError()
        self.parent.update(self.entries)


@contextmanager
def publication_group() -> Iterator[_PublicationGroup]:
    tail = _tail.get()
    parent = _publication_group.get()
    if parent is None and tail is not None:
        parent = tail.publications
    group = _PublicationGroup(tail, parent)
    token = _publication_group.set(group.entries)
    try:
        yield group
    finally:
        _publication_group.reset(token)


def retain_publication(
    owner: object, check: Callable[[], Awaitable[Any]], fresh: Callable[[], bool]
) -> bool:
    """Retain strict final-response evidence in its original authorization context."""
    tail = _tail.get()
    if tail is None:
        return False
    if tail.closed:
        raise RequiredTailError()
    target = _publication_group.get()
    if target is None:
        target = tail.publications
    target[id(owner)] = (owner, copy_context(), check, fresh)
    return True


def discard_publication(owner: object) -> None:
    """Forget evidence when its whole private contribution was discarded."""
    tail = _tail.get()
    if tail is not None and not tail.closed:
        target = _publication_group.get()
        if target is None:
            target = tail.publications
        target.pop(id(owner), None)


async def _publication_check(context: Context, check: Callable[[], Awaitable[Any]]) -> None:
    async def renew() -> None:
        deadline = min(
            REQUIRED_STEP_TIMEOUT_S,
            class_timeout_s("read") or REQUIRED_STEP_TIMEOUT_S,
        )
        with query_tag("knowledge.mirror.publication"), statement_timeout(deadline):
            async with asyncio.timeout(REQUIRED_STEP_TIMEOUT_S):
                await check()

    # The handler's auth context has unwound by ASGI publication time.
    task = asyncio.eager_task_factory(
        asyncio.get_running_loop(),
        renew(),
        context=context.copy(),
        name="request.publication-proof",
    )
    caller = asyncio.current_task()
    cancellations = caller.cancelling() if caller is not None else 0
    try:
        await task
    except asyncio.CancelledError:
        if caller is not None and caller.cancelling() > cancellations:
            raise
        raise RequiredTailError() from None
    except Exception:  # noqa: BLE001 — strict renewal exposes no private backend detail
        raise RequiredTailError() from None


async def _publication_barrier(tail: _Tail) -> None:
    """Renew retained inputs after accounting and after each awaited send boundary."""
    if not tail.publications:
        return
    checks = tuple(tail.publications.values())
    try:
        for _owner, context, check, _fresh in checks:
            await _publication_check(context, check)
        # A later renewal can outlive an earlier proof's deadline.
        if not all(context.run(fresh) for _owner, context, _check, fresh in checks):
            raise RequiredTailError()
    except Exception:  # noqa: BLE001 — publication failures expose only a constant refusal
        tail.required_failed = True
        raise RequiredTailError() from None
    except BaseException:
        tail.required_failed = True
        raise


async def _required_step(tail: _Tail, tag: str, step: Callable[[], Awaitable[Any]]) -> None:
    succeeded = False
    try:
        deadlines = [REQUIRED_STEP_TIMEOUT_S]
        deadlines.extend(
            value for kind in ("read", "write") if (value := class_timeout_s(kind)) > 0
        )
        with query_tag(tag), statement_timeout(min(deadlines)):
            async with asyncio.timeout(REQUIRED_STEP_TIMEOUT_S):
                try:
                    await step()
                finally:
                    await asyncio.sleep(0)
        succeeded = True
    except BaseException:  # noqa: BLE001 — required callback outcomes never expose private details
        pass
    finally:
        if not succeeded:
            tail.required_failed = True


async def _ordinary_step(tail: _Tail, tag: str, step: Callable[[], Awaitable[Any]]) -> None:
    try:
        with query_tag(tag):
            try:
                await step()
            finally:
                if tail.has_required:
                    await asyncio.sleep(0)
    except Exception as error:  # noqa: BLE001 — ordinary cleanup stays best effort
        log.warning("request tail step failed: %s", error)


async def _drain(tail: _Tail, required_only: bool) -> None:
    while True:
        try:
            await tail.lock.acquire()
            break
        except asyncio.CancelledError:
            if not tail.has_required:
                raise
            tail.required_failed = True
    try:
        if tail.completed:
            if tail.required_failed:
                raise RequiredTailError() from None
            return
        control: BaseException | None = None
        try:
            while tail.steps:
                index = 0
                if required_only:
                    selected = next((i for i, step in enumerate(tail.steps) if step[2]), None)
                    if selected is None:
                        break
                    index = selected
                tag, step, required = tail.steps.pop(index)
                try:
                    if required:
                        await _required_step(tail, tag, step)
                    else:
                        await _ordinary_step(tail, tag, step)
                except BaseException as error:  # marked cleanup must finish before control returns
                    if not tail.has_required:
                        raise
                    if control is None:
                        control = error
        finally:
            if not required_only and not tail.steps:
                tail.completed = True
        if control is not None:
            raise control from None
        if tail.required_failed:
            raise RequiredTailError() from None
    finally:
        tail.lock.release()


async def _owned_drain(tail: _Tail, required_only: bool) -> None:
    async def attempt() -> BaseException | None:
        try:
            with anyio.CancelScope(shield=True):
                await _drain(tail, required_only)
        except BaseException as error:  # noqa: BLE001 — separate task and waiter cancellation
            return error
        return None

    cancelled: asyncio.CancelledError | None = None
    caller = asyncio.current_task()
    cancellations = caller.cancelling() if caller is not None else 0
    with anyio.CancelScope(shield=True):
        # Enter the ownership wrapper in a distinct task before its first suspension.
        task = asyncio.eager_task_factory(
            asyncio.get_running_loop(), attempt(), name="request.required-tail"
        )
        while True:
            try:
                failure = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                if task.cancelled():
                    tail.required_failed = True
                    failure = RequiredTailError()
                    if caller is not None and caller.cancelling() > cancellations:
                        cancelled = cancelled or asyncio.CancelledError()
                    break
                if cancelled is None:
                    cancelled = error
    try:
        await anyio.lowlevel.checkpoint_if_cancelled()
    except asyncio.CancelledError as error:
        if cancelled is None:
            cancelled = error
    if cancelled is not None:
        raise cancelled from None
    if failure is not None:
        raise failure from None


async def drain_required() -> None:
    """Commit required work between logical calls without closing ordinary cleanup."""
    tail = _tail.get()
    if tail is not None and tail.has_required:
        await _owned_drain(tail, True)


async def settle(tail: _Tail) -> None:
    """Close registration and finish remaining steps, retaining required failure."""
    if tail.closed and not tail.has_required:
        return
    tail.closed = True
    if tail.has_required:
        await _owned_drain(tail, False)
    else:
        await _drain(tail, False)


async def settle_current() -> None:
    tail = _tail.get()
    if tail is not None:
        await settle(tail)


async def publish_current() -> None:
    """Settle streamed accounting and renew its retained source proofs."""
    tail = _tail.get()
    if tail is not None:
        await settle(tail)
        await _publication_barrier(tail)


@asynccontextmanager
async def streaming_scope() -> AsyncIterator[None]:
    """Own final streamed usage after the HTTP header tail has closed.

    The caller settles before its terminal event. Cancellation still joins the
    owned required delivery before this scope returns.
    """
    parent = _tail.get()
    tail = _Tail()
    if parent is not None:
        # Every HTTP send must see proofs retained during model generation.
        tail.publications = parent.publications
    token = _tail.set(tail)
    group_token = _publication_group.set(None)
    primary: BaseException | None = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            await settle(tail)
        except BaseException:
            if primary is None:
                raise
        finally:
            _publication_group.reset(group_token)
            _tail.reset(token)


class RequestTailMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        tail = _Tail()
        token = _tail.set(tail)
        primary: BaseException | None = None

        async def _send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                await settle(tail)
                await _publication_barrier(tail)
            elif message.get("type") == "http.response.body" and message.get("body"):
                # Empty completion frames publish no bytes; the client may already
                # have consumed Content-Length and started its next request.
                await _publication_barrier(tail)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except BaseException as error:  # preserve the application's error or cancellation
            primary = error
            raise
        finally:
            try:
                try:
                    await settle(tail)
                except BaseException:  # cleanup failure must not replace a primary outcome
                    if primary is None:
                        raise
            finally:
                _tail.reset(token)


__all__ = [
    "RequestTailMiddleware",
    "RequiredTailError",
    "active",
    "defer",
    "defer_once",
    "defer_required",
    "publication_group",
    "retain_publication",
    "discard_publication",
    "deferred",
    "drain_required",
    "required_active",
    "fail_required",
    "settle",
    "settle_current",
    "publish_current",
]
