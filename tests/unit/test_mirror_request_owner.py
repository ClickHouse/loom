"""HTTP ownership survives logical-bag replacement and middleware task boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from server.core import request_work
from server.core.diagnostics import private_work
from server.core.request_tail import RequestTailMiddleware
from server.transport.request_scope import HTTPApplication

_OWNER_KEY = "loom.request_work_owner"


@pytest.fixture(autouse=True)
def instrumentor_background_state():
    # Uninstrumenting a local app also removes the process-wide background hook.
    missing = object()
    original_call = BackgroundTask.__call__
    original_hook = vars(BackgroundTask).get("_otel_original_call", missing)
    try:
        yield
    finally:
        BackgroundTask.__call__ = original_call
        if original_hook is missing:
            if "_otel_original_call" in vars(BackgroundTask):
                delattr(BackgroundTask, "_otel_original_call")
        else:
            BackgroundTask._otel_original_call = original_hook


def flags(scope):
    owner = scope.get(_OWNER_KEY)
    return bool(owner and owner.mirror_marked), bool(owner and owner.mirror_unread)


@pytest.fixture
def world():
    token = request_work._current.set(None)
    state = SimpleNamespace(seen=[], scopes=[])
    yield state
    request_work._current.reset(token)


def owner_application(endpoint):
    app = HTTPApplication()
    app.mount("/", RequestTailMiddleware(endpoint))
    return app


def application(state, endpoint):
    async def observe(request, call_next):
        try:
            return await call_next(request)
        finally:
            state.seen.append((request.url.path, flags(request.scope)))

    inner = BaseHTTPMiddleware(endpoint, dispatch=observe)
    outer = BaseHTTPMiddleware(inner, dispatch=observe)
    return owner_application(outer)


@pytest.mark.parametrize("unread", [False, True])
@pytest.mark.parametrize("replace_child_bag", [False, True])
async def test_http_owner_survives_private_child_and_bag_replacement(
    world, unread, replace_child_bag
):
    async def endpoint(scope, receive, send):
        world.scopes.append(scope)
        parent = request_work.begin()

        async def child():
            if replace_child_bag:
                request_work.begin()
            with private_work():
                assert request_work.current() is None
                assert request_work.mark_mirrored(unread=unread)

        task = asyncio.create_task(child())
        try:
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert parent.mirror_marked is (not replace_child_bag)
        assert flags(scope) == (True, unread)
        # A later logical operation has independent counters and local flags.
        next_bag = request_work.begin()
        assert next_bag is not parent and not request_work.mirror_marked()
        assert next_bag.ch_statements == 0
        assert flags(scope) == (True, unread)
        await PlainTextResponse("ready")(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(world, endpoint)), base_url="http://test"
    ) as client:
        response = await client.get("/marked")
    assert response.status_code == 200 and response.text == "ready"
    assert world.seen == [("/marked", (True, unread)), ("/marked", (True, unread))]
    assert flags(world.scopes[0]) == (True, unread)
    assert request_work._current.get() is None


async def test_sequential_http_requests_keep_owner_and_accounting_separate(world):
    async def endpoint(scope, receive, send):
        world.scopes.append(scope)
        assert flags(scope) == (False, False)
        assert request_work._current.get() is None
        bag = request_work.begin()
        if scope["path"] == "/marked":
            assert request_work.mark_mirrored(unread=True)
            bag.ch_statements = 11
        else:
            assert not request_work.mirror_marked() and bag.ch_statements == 0
        await PlainTextResponse("ready")(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(world, endpoint)), base_url="http://test"
    ) as client:
        assert (await client.get("/marked")).status_code == 200
        assert (await client.get("/ordinary")).status_code == 200
    assert world.scopes[0].get(_OWNER_KEY) is not world.scopes[1].get(_OWNER_KEY)
    assert world.seen == [
        ("/marked", (True, True)),
        ("/marked", (True, True)),
        ("/ordinary", (False, False)),
        ("/ordinary", (False, False)),
    ]
    assert request_work._current.get() is None


async def test_overlapping_http_requests_have_distinct_owners(world):
    marked = asyncio.Event()
    ordinary = asyncio.Event()

    async def endpoint(scope, receive, send):
        world.scopes.append(scope)
        request_work.begin()
        if scope["path"] == "/marked":
            assert request_work.mark_mirrored()
            marked.set()
            await asyncio.wait_for(ordinary.wait(), timeout=5)
            assert flags(scope) == (True, False)
        else:
            await asyncio.wait_for(marked.wait(), timeout=5)
            assert not request_work.mirror_marked() and flags(scope) == (False, False)
            ordinary.set()
        await PlainTextResponse("ready")(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(world, endpoint)), base_url="http://test"
    ) as client:
        tasks = [asyncio.create_task(client.get(path)) for path in ("/marked", "/ordinary")]
        try:
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
            assert [response.status_code for response in responses] == [200, 200]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    by_path = {scope["path"]: scope for scope in world.scopes}
    assert by_path["/marked"].get(_OWNER_KEY) is not by_path["/ordinary"].get(_OWNER_KEY)
    assert sorted(world.seen) == [
        ("/marked", (True, False)),
        ("/marked", (True, False)),
        ("/ordinary", (False, False)),
        ("/ordinary", (False, False)),
    ]
    assert request_work._current.get() is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_http_owner_restores_context_after_endpoint_failure(world, cancel):
    from server.core.request_errors import MarkedRequestError

    class EndpointFailure(Exception):
        pass

    failure = asyncio.CancelledError() if cancel else EndpointFailure()
    prior = request_work.begin()

    async def endpoint(scope, receive, send):
        world.scopes.append(scope)
        assert request_work._current.get() is None
        request_work.begin()
        assert request_work.mark_mirrored(unread=False)
        raise failure

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_application(endpoint)), base_url="http://test"
    ) as client:
        with pytest.raises(asyncio.CancelledError if cancel else MarkedRequestError) as caught:
            await client.get("/failed")
    if cancel:
        assert caught.value is failure
    else:
        assert type(caught.value) is MarkedRequestError
        assert caught.value.__context__ is failure
        assert caught.value.__suppress_context__
        assert str(caught.value) == "Private request failed"
    assert flags(world.scopes[0]) == (True, False)
    assert request_work._current.get() is prior and not prior.mirror_marked

    request_work.begin()
    assert request_work.mark_mirrored(unread=True)
    assert flags(world.scopes[0]) == (True, False)


def test_worker_bag_without_http_owner_remains_fresh(world):
    first = request_work.begin()
    assert request_work.mark_mirrored(unread=True)
    second = request_work.begin()
    assert first is not second and first.mirror_marked and first.mirror_unread
    assert not request_work.mirror_marked() and not second.mirror_unread


async def test_http_owner_unread_is_monotonic_across_distinct_logical_bags(world):
    async def endpoint(scope, receive, send):
        world.scopes.append(scope)
        request_work.begin()
        assert request_work.mark_mirrored(unread=True)
        assert flags(scope) == (True, True)
        next_bag = request_work.begin()
        assert request_work.mark_mirrored(unread=False)
        assert next_bag.mirror_marked and not next_bag.mirror_unread
        assert flags(scope) == (True, True)
        await PlainTextResponse("ready")(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(world, endpoint)), base_url="http://test"
    ) as client:
        assert (await client.get("/marked")).status_code == 200
    assert world.seen == [("/marked", (True, True)), ("/marked", (True, True))]


async def test_outside_request_mark_cannot_mutate_a_completed_http_owner(world):
    async def endpoint(scope, receive, send):
        world.scopes.append(scope)
        request_work.begin()
        await PlainTextResponse("ready")(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_application(endpoint)), base_url="http://test"
    ) as client:
        assert (await client.get("/ordinary")).status_code == 200
    completed = world.scopes[0]
    assert flags(completed) == (False, False)
    request_work.begin()
    assert request_work.mark_mirrored(unread=True)
    assert flags(completed) == (False, False)


async def test_owner_survives_installed_instrumentor_finalizers(world):
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON
    from opentelemetry.trace import SpanKind

    ended = []
    histograms = []
    active = []

    class Observer(SpanProcessor):
        def on_end(self, span):
            ended.append((span.kind, request_work.http_mirror_marked()))

    class Histogram:
        def __init__(self, name):
            self.name = name

        def record(self, value, attributes=None, context=None):
            histograms.append((self.name, value, request_work.http_mirror_marked()))

    class Counter:
        def add(self, value, attributes=None, context=None):
            active.append((value, request_work.http_mirror_marked()))

    class Meter:
        def create_histogram(self, name, *args, **kwargs):
            return Histogram(name)

        def create_up_down_counter(self, name, *args, **kwargs):
            return Counter()

    class Meters:
        def get_meter(self, *args, **kwargs):
            return Meter()

    async def endpoint(scope, receive, send):
        world.scopes.append(scope)
        request_work.begin()
        assert request_work.mark_mirrored()
        await PlainTextResponse("ready")(scope, receive, send)

    app = owner_application(endpoint)
    provider = TracerProvider(sampler=ALWAYS_ON, resource=Resource({}), shutdown_on_exit=False)
    provider.add_span_processor(Observer())
    try:
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=provider, meter_provider=Meters(), excluded_urls=""
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/marked")
        assert response.status_code == 200 and response.text == "ready"
        assert [marked for kind, marked in ended if kind is SpanKind.SERVER] == [True]
        assert histograms and all(marked for _name, _value, marked in histograms)
        assert active and sum(value for value, _marked in active) == 0
        assert any(value > 0 and not marked for value, marked in active)
        assert any(value < 0 and marked for value, marked in active)
        assert not request_work.http_mirror_marked()
        assert request_work._current.get() is None
    finally:
        try:
            FastAPIInstrumentor.uninstrument_app(app)
        finally:
            provider.shutdown()


async def test_lifespan_delegates_without_an_http_owner(world):
    app = HTTPApplication()
    prior = request_work.begin()
    received = iter(({"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}))
    sent = []
    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}

    async def receive():
        return next(received)

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    assert sent == [{"type": "lifespan.startup.complete"}, {"type": "lifespan.shutdown.complete"}]
    assert _OWNER_KEY not in scope and not request_work.http_mirror_marked()
    assert request_work._current.get() is prior


def test_application_keeps_fastapi_routing_and_schema(world):
    from fastapi import FastAPI

    app = HTTPApplication(title="Example")

    @app.get("/ping")
    async def ping():
        return {"ready": True}

    assert isinstance(app, FastAPI)
    schema = app.openapi()
    assert schema["info"]["title"] == "Example"
    assert schema["paths"]["/ping"]["get"]["responses"]["200"]["description"]
