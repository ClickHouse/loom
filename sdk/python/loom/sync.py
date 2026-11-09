"""Synchronous facade over the async SDK.

``SyncAgent`` is the blocking counterpart to ``Agent`` for callers that can't
await (e.g. a daemon calling ``memory.search`` from worker threads). It runs a
single private event loop on one background thread and marshals every call onto
it, sharing one ``httpx.AsyncClient`` connection pool across all calling
threads. **Construct one SyncAgent per process and share it** — a SyncAgent per
call spins up a loop thread and a fresh pool every time.

    with SyncAgent(org="acme", namespace="ci-triage", agent="analyst") as a:
        a.memory.set("last_pr", 12345)
        hits = a.memory.search("flaky test in MergeTree", top_k=20)
        for h in hits:
            print(h.memory_key, h.similarity)
        with a.run_scope(goal="triage the flaky test") as run:
            answer = a.memory.ask("what broke CI last week?")     # cited keys → memories_used
            run.used("ci:flaky:2026-09")

The proxied clients (``a.memory``, ``a.knowledge``, …) mirror the async
clients 1:1 (see ``_SyncClientProxy``).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import Future
from types import TracebackType
from typing import Any, Concatenate, ParamSpec, TypeVar

from loom.agent import Agent
from loom.retry import RetryPolicy
from loom.run import _ACTIVE_RUN, RunClient, RunScope

# Client attributes on Agent that get a synchronous proxy.
_CLIENT_ATTRS: tuple[str, ...] = (
    # Every op as a generated method; its groups are nested clients.
    "client",
    "account",
    "deployment",
    "memory",
    "working_memory",
    "briefs",
    "knowledge",
    # The Loom assistant: `sa.assistant.stream(...)` is a blocking iterator.
    "assistant",
    "conversations",
    "learnings",
    "conflicts",
    "blacklist",
    "profile",
    "grants",
    "erasure",
    "cost",
    "audit",
    "facts",
    "namespaces",
    "procedure",
    "calibration",
    "context",
    # The run ledger: `a.run.begin / complete / get / list`
    # block like every other client; `a.run_scope(...)` is the `with` form.
    "run",
    "tasks",
    "code",
    "policy",
    "decision",
    "tracker",
)

_P = ParamSpec("_P")
_R = TypeVar("_R")

# Bound on close()'s wait for the transport to drain.
_CLOSE_TIMEOUT_S = 10.0


# The loop thread and the finaliser are module-level so neither holds the
# SyncAgent: an unclosed one is still collected, and its finaliser runs.
def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5.0)
    if not loop.is_running() and not loop.is_closed():
        loop.close()


def _abandon(loop: asyncio.AbstractEventLoop, thread: threading.Thread, agent: Agent) -> None:
    """Finaliser of a SyncAgent nobody closed: release the pool and the loop
    thread. No flush and no run completion — that is `close()`'s job."""
    if threading.current_thread() is thread:
        loop.stop()
        return
    try:
        asyncio.run_coroutine_threadsafe(agent.transport.close(), loop).result(_CLOSE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — a finaliser never raises
        pass
    _stop_loop(loop, thread)


class _SyncClientProxy:
    """Wraps one async client (e.g. ``MemoryClient``) and turns each of its
    coroutine methods into a blocking call scheduled on the owner's loop,
    each async generator into a blocking iterator, and an async context
    manager into a ``with`` block.

    Non-coroutine attributes (properties, constants, sync helpers) pass
    through unchanged. Wrapped methods are cached so identity is stable.
    """

    def __init__(self, owner: SyncAgent, client: object) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_wrapped", {})

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        cache: dict[str, Any] = self._wrapped
        if getattr(attr, "_loom_nested_client", False):
            # A client inside a client (`memory.record`, `memory.schema`):
            # its coroutines block the same way, one proxy per nested object.
            if name not in cache:
                cache[name] = _SyncClientProxy(self._owner, attr)
            return cache[name]
        if name == "scope" and isinstance(self._client, RunClient):
            # The run's ContextVar must be set in the calling thread.
            return self._owner.run_scope
        if inspect.isasyncgenfunction(attr):
            if name not in cache:
                cache[name] = functools.wraps(attr)(functools.partial(self._iterate, attr))
            return cache[name]
        if not inspect.iscoroutinefunction(attr):
            return attr
        if name not in cache:

            @functools.wraps(attr)
            def _blocking(*args: Any, **kwargs: Any) -> Any:
                return self._owner._run(attr(*args, **kwargs))

            cache[name] = _blocking
        return cache[name]

    def _iterate(self, fn: Any, *args: Any, **kwargs: Any) -> Iterator[Any]:
        agen = fn(*args, **kwargs)
        try:
            while True:
                try:
                    yield self._owner._run(agen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            if not self._owner._closed:
                self._owner._run(agen.aclose())

    def __enter__(self) -> _SyncClientProxy:
        self._owner._run(self._client.__aenter__())
        return self

    def __exit__(self, *exc: object) -> None:
        self._owner._run(self._client.__aexit__(*exc))

    def __dir__(self) -> list[str]:
        return sorted(set(dir(self._client)) | set(object.__dir__(self)))


def _blocking(
    fn: Callable[Concatenate[Agent, _P], Awaitable[_R]],
) -> Callable[Concatenate[SyncAgent, _P], _R]:
    """The `Agent` coroutine method `fn` as a blocking `SyncAgent` method."""

    @functools.wraps(fn)
    def method(self: SyncAgent, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        out: _R = self._run(fn(self._agent, *args, **kwargs))
        return out

    return method


def _proxied(
    fn: Callable[Concatenate[Agent, _P], object],
) -> Callable[Concatenate[SyncAgent, _P], Any]:
    """The `Agent` factory `fn`, its result behind a blocking proxy."""

    @functools.wraps(fn)
    def method(self: SyncAgent, /, *args: _P.args, **kwargs: _P.kwargs) -> Any:
        return _SyncClientProxy(self, fn(self._agent, *args, **kwargs))

    return method


class SyncAgent:
    """Blocking counterpart to :class:`loom.Agent`.

    Same constructor arguments as ``Agent`` plus ``call_timeout_s`` — a hard
    ceiling on how long any one blocking call waits for the loop before
    raising ``concurrent.futures.TimeoutError`` (the underlying coroutine
    keeps running). ``None`` (default) blocks until the call completes; the
    transport already enforces its own per-request timeout + retries.
    """

    # The blocking client proxies `__init__` attaches, one per `_CLIENT_ATTRS`
    # name, declared so a type checker sees `sa.memory.set(...)`.
    client: _SyncClientProxy
    account: _SyncClientProxy
    deployment: _SyncClientProxy
    memory: _SyncClientProxy
    working_memory: _SyncClientProxy
    briefs: _SyncClientProxy
    knowledge: _SyncClientProxy
    assistant: _SyncClientProxy
    conversations: _SyncClientProxy
    learnings: _SyncClientProxy
    conflicts: _SyncClientProxy
    blacklist: _SyncClientProxy
    profile: _SyncClientProxy
    grants: _SyncClientProxy
    erasure: _SyncClientProxy
    cost: _SyncClientProxy
    audit: _SyncClientProxy
    facts: _SyncClientProxy
    namespaces: _SyncClientProxy
    procedure: _SyncClientProxy
    calibration: _SyncClientProxy
    context: _SyncClientProxy
    run: _SyncClientProxy
    tasks: _SyncClientProxy
    code: _SyncClientProxy
    policy: _SyncClientProxy
    decision: _SyncClientProxy
    tracker: _SyncClientProxy

    def __init__(
        self,
        org: str,
        namespace: str,
        agent: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        pacing: bool = True,
        coalesce_sets: bool = True,
        ratelimit_max_wait_s: float | None = None,
        call_timeout_s: float | None = None,
        auto_run: bool | None = None,
        retry: RetryPolicy | dict[str, Any] | None = None,
        consumer: str = "",
        mentor_brief: bool | None = None,
        profile: str | None = None,
    ) -> None:
        self._closed = False
        self._close_lock = threading.Lock()
        self._call_timeout_s = call_timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=_run_loop, args=(self._loop,), name="loom-sync-loop", daemon=True
        )
        self._thread.start()

        try:
            # Build the Agent on the loop thread so its httpx.AsyncClient binds
            # to the loop it will actually run on.
            async def _build() -> Agent:
                return Agent(
                    org,
                    namespace,
                    agent,
                    base_url=base_url,
                    api_key=api_key,
                    timeout_s=timeout_s,
                    pacing=pacing,
                    auto_run=auto_run,
                    coalesce_sets=coalesce_sets,
                    ratelimit_max_wait_s=ratelimit_max_wait_s,
                    retry=retry,
                    consumer=consumer,
                    mentor_brief=mentor_brief,
                    profile=profile,
                )

            self._agent: Agent = self._submit(_build()).result(_CLOSE_TIMEOUT_S)
        except BaseException:
            _stop_loop(self._loop, self._thread)  # don't leak the loop thread
            raise
        self._finalizer = weakref.finalize(self, _abandon, self._loop, self._thread, self._agent)

        for name in _CLIENT_ATTRS:
            client = getattr(self._agent, name, None)
            if client is not None:
                setattr(self, name, _SyncClientProxy(self, client))

    # --- identity passthrough (parity with Agent) ----------------------
    @property
    def org(self) -> str:
        return self._agent.org

    @property
    def namespace(self) -> str:
        return self._agent.namespace

    @property
    def agent(self) -> str:
        return self._agent.agent

    @property
    def consumer(self) -> str:
        """The consumer label stamped on every body; '' = none."""
        return self._agent.consumer

    @property
    def transport(self) -> Any:
        return self._agent.transport

    @property
    def auto_run(self) -> bool:
        return self._agent.auto_run

    @property
    def ambient_run_id(self) -> str:
        """See `Agent.ambient_run_id`."""
        return self._agent.ambient_run_id

    @property
    def coalesce_sets(self) -> bool:
        return self._agent.coalesce_sets

    @property
    def mentor_brief(self) -> bool:
        return self._agent.mentor_brief

    # --- the generic call and the probe (parity with Agent) --
    def identity(self, **scope: Any) -> dict[str, str]:
        """See `Agent.identity`."""
        return self._agent.identity(**scope)

    # Agent's own signatures and docstrings, blocking.
    call = _blocking(Agent.call)
    call_with_meta = _blocking(Agent.call_with_meta)
    mentor = _blocking(Agent.mentor)
    guide = _blocking(Agent.guide)
    probe = _blocking(Agent.probe)
    capabilities = _blocking(Agent.capabilities)
    can_call = _blocking(Agent.can_call)
    # A `Session` / `AgentTools` whose coroutine methods block;
    # `with sa.session(...) as s:` flushes on exit.
    session = _proxied(Agent.session)
    tools = _proxied(Agent.tools)

    def run_scope(self, **begin_kwargs: Any) -> SyncRunScope:
        """One agent execution as a `with` block — the blocking counterpart
        of `Agent.run_scope`: `run.begin` on entry,
        `run.complete` on exit with the keys the block's `memory.ask`
        answers cited plus anything passed to `used()`. Memory calls made
        from the entering thread inside the block carry the run_id."""
        return SyncRunScope(self, **begin_kwargs)

    # --- loop plumbing -------------------------------------------------
    def _submit(self, coro: Any) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _run(self, coro: Any) -> Any:
        if self._closed:
            coro.close()  # avoid "coroutine was never awaited" warning
            raise RuntimeError("SyncAgent is closed")
        return self._submit(coro).result(self._call_timeout_s)

    # --- lifecycle -----------------------------------------------------
    def close(self, failed: bool = False) -> None:
        """`Agent.close` on the loop, then stop the loop thread. Idempotent
        and safe from any thread but the loop's own (a callback running on
        the loop cannot wait for it)."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("SyncAgent.close() cannot run on its own loop thread")
        with self._close_lock:
            if self._closed:
                return
            try:
                self._submit(self._agent.close(failed=failed)).result(_CLOSE_TIMEOUT_S)
            finally:
                self._closed = True
                self._finalizer.detach()
                _stop_loop(self._loop, self._thread)

    def __enter__(self) -> SyncAgent:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close(failed=exc_type is not None)


class SyncRunScope:
    """Blocking `with` form of :class:`loom.run.RunScope`.

    The run is opened and closed on the SyncAgent's loop; the active-run
    ContextVar is set in the *calling* thread, which is the context
    `run_coroutine_threadsafe` copies into every call the thread makes, so
    `memory.set` / `memory.search` / `memory.ask` from that thread stamp the
    run_id and `ask` credits its cited keys. Yields the underlying RunScope
    (`used()`, `final_response`, `confidence`, `outcome`, `status`, `completed`)."""

    def __init__(self, owner: SyncAgent, **begin_kwargs: Any) -> None:
        self._owner = owner
        self._scope = RunScope(owner._agent.run, **begin_kwargs)
        self._token: Any = None

    def __enter__(self) -> RunScope:
        scope = self._scope
        res = self._owner._run(scope._client.begin(**scope._begin_kwargs))
        scope.run_id = str(res.get("run_id") or scope.run_id)
        scope._t0 = time.perf_counter()
        self._token = _ACTIVE_RUN.set(scope)
        return scope

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._token is not None:
            _ACTIVE_RUN.reset(self._token)
            self._token = None
        # RunScope.__aexit__ completes the run (its own token is unset here,
        # so it touches no ContextVar) and never masks the block's exception.
        self._owner._run(self._scope.__aexit__(exc_type, exc_val, exc_tb))
