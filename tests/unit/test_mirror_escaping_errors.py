"""Private escapes preserve control flow and are dropped before log formatting."""

from __future__ import annotations

import asyncio
import io
import logging

import pytest
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from server.core import diagnostics, logging_config, request_work
from server.core.request_errors import MarkedRequestError, mark_preserved_error, marked_error
from server.transport.request_scope import HTTPApplication


@pytest.fixture(autouse=True)
def isolated_request_state():
    local = request_work._current.set(None)
    owner = request_work._http_owner.set(None)
    private = diagnostics._codes_only.set(False)
    try:
        yield
    finally:
        diagnostics._codes_only.reset(private)
        request_work._http_owner.reset(owner)
        request_work._current.reset(local)


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("headers_sent", [False, True])
async def test_application_error_boundary(monkeypatch, marked, headers_sent):
    original = ValueError("private application error canary")
    sent = []

    async def send(message):
        sent.append(message)

    async def invoke(self, scope, receive, send):
        request_work.begin()
        if marked:
            assert request_work.mark_mirrored()
        if headers_sent:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise original

    monkeypatch.setattr(FastAPI, "__call__", invoke)
    with pytest.raises(MarkedRequestError if marked else ValueError) as caught:
        await HTTPApplication()({"type": "http"}, None, send)
    assert len(sent) == int(headers_sent)
    assert not request_work.http_mirror_marked()
    assert request_work.current() is None
    if marked:
        assert caught.value.args == ("Private request failed",)
        assert caught.value.__context__ is original
        assert caught.value.__suppress_context__ is True
        assert marked_error(caught.value)
    else:
        assert caught.value is original
        assert not marked_error(original)


def control_errors():
    return [
        asyncio.CancelledError("private cancellation canary"),
        ClientDisconnect("private disconnect canary"),
        OSError("private OS canary"),
        EndOfStream("private end canary"),
        BrokenResourceError("private broken resource canary"),
        ClosedResourceError("private closed resource canary"),
        ExceptionGroup("private group canary", [ValueError("private leaf canary")]),
        BaseExceptionGroup(
            "private control group canary", [asyncio.CancelledError("private child canary")]
        ),
        KeyboardInterrupt("private process canary"),
        SystemExit("private exit canary"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("position", range(10))
async def test_control_and_group_identity(monkeypatch, marked, position):
    original = control_errors()[position]
    children = original.exceptions if isinstance(original, BaseExceptionGroup) else None

    async def invoke(self, scope, receive, send):
        request_work.begin()
        if marked:
            assert request_work.mark_mirrored()
        raise original

    monkeypatch.setattr(FastAPI, "__call__", invoke)
    with pytest.raises(type(original)) as caught:
        await HTTPApplication()({"type": "http"}, None, None)
    assert caught.value is original
    if children is not None:
        assert caught.value.exceptions is children
        assert all(a is b for a, b in zip(caught.value.exceptions, children, strict=True))
    assert marked_error(caught.value) is marked
    assert not request_work.http_mirror_marked()
    assert request_work.current() is None
    assert not marked_error(ValueError("separate ordinary error"))


def test_control_marker_bypasses_exception_attribute_overrides():
    class Controlled(OSError):
        def __getattribute__(self, name):
            if name.startswith("_loom_"):
                raise AssertionError("subclass getter must not run")
            return super().__getattribute__(name)

        def __setattr__(self, name, value):
            raise AssertionError("subclass setter must not run")

    original = Controlled("private control canary")
    mark_preserved_error(original)
    mark_preserved_error(original)
    assert marked_error(original)


class CountingFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(message)s")
        self.calls = 0

    def format(self, record):
        self.calls += 1
        return super().format(record)


@pytest.fixture
def serving_logs(monkeypatch):
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
    loggers = [logging.getLogger(name) for name in names]
    snapshots = [
        (
            logger,
            logger.level,
            logger.handlers[:],
            logger.filters[:],
            logger.propagate,
            logger.disabled,
        )
        for logger in loggers
    ]
    root_handlers = [*logging.getLogger().handlers, logging.lastResort]
    handler_filters = [(handler, handler.filters[:]) for handler in root_handlers if handler]
    outputs = {}
    try:
        for logger in loggers:
            stream, formatter = io.StringIO(), CountingFormatter()
            handler = logging.StreamHandler(stream)
            handler.setFormatter(formatter)
            logger.handlers, logger.filters = [handler], []
            logger.propagate, logger.disabled = False, False
            logger.setLevel(logging.INFO)
            outputs[logger.name] = (logger, stream, formatter)
        monkeypatch.setattr(logging_config, "_configured", True)
        logging_config.configure_logging()
        logging_config.configure_logging()
        yield outputs
    finally:
        for logger, level, handlers, filters, propagate, disabled in snapshots:
            for handler in logger.handlers:
                if handler not in handlers:
                    handler.close()
            logger.setLevel(level)
            logger.handlers, logger.filters = handlers, filters
            logger.propagate, logger.disabled = propagate, disabled
        for handler, filters in handler_filters:
            handler.filters[:] = filters


def test_protocol_error_and_access_after_owner_reset(serving_logs):
    error, error_stream, error_formatter = serving_logs["uvicorn.error"]
    access, access_stream, access_formatter = serving_logs["uvicorn.access"]
    original = OSError("private fallback canary")
    mark_preserved_error(original)
    assert not request_work.http_mirror_marked()
    try:
        raise original
    except OSError:
        error.error("protocol error canary", exc_info=True)
        access.info("fallback500 access canary")
    assert error_stream.getvalue() == access_stream.getvalue() == ""
    assert error_formatter.calls == access_formatter.calls == 0
    error.info("ordinary transport control")
    access.info("ordinary access control")
    assert error_stream.getvalue() == "ordinary transport control\n"
    assert access_stream.getvalue() == "ordinary access control\n"
    assert error_formatter.calls == access_formatter.calls == 1
    assert error.filters.count(diagnostics.public_log) == 1
    assert access.filters.count(diagnostics.public_log) == 1


def test_exception_record_is_private_outside_caught_exception(serving_logs):
    logger, stream, formatter = serving_logs["uvicorn.error"]
    original = MarkedRequestError()
    original.__cause__ = ValueError("private cause canary")
    original.add_note("private note canary")
    logger.error("private outcome canary", exc_info=(type(original), original, None))
    assert stream.getvalue() == ""
    assert formatter.calls == 0


def test_verbose_asgi_source_is_suppressed_for_ordinary_requests(serving_logs):
    logger, stream, formatter = serving_logs["uvicorn.asgi"]
    assert not request_work.http_mirror_marked()
    for phase in ("Started", "Receive", "Send", "Completed", "Raised exception"):
        logger.info("%s private message canary", phase)
    assert stream.getvalue() == ""
    assert formatter.calls == 0
    assert len(logger.filters) == 1


def test_verbose_filter_preserves_shared_handler_ordinary_logs(serving_logs):
    verbose, verbose_stream, verbose_formatter = serving_logs["uvicorn.asgi"]
    error, error_stream, error_formatter = serving_logs["uvicorn.error"]
    error.addHandler(verbose.handlers[0])
    logging_config.configure_logging()
    verbose.info("verbose request message canary")
    assert verbose_stream.getvalue() == error_stream.getvalue() == ""
    assert verbose_formatter.calls == error_formatter.calls == 0
    error.info("ordinary shared-handler control")
    assert (
        verbose_stream.getvalue() == error_stream.getvalue() == "ordinary shared-handler control\n"
    )
    assert verbose_formatter.calls == error_formatter.calls == 1


def test_classification_failure_drops_before_formatter(monkeypatch, serving_logs):
    from server.core import request_errors

    def unavailable(error):
        raise RuntimeError("private classifier failure canary")

    logger, stream, formatter = serving_logs["uvicorn.error"]
    monkeypatch.setattr(request_errors, "marked_error", unavailable)
    logger.error("private diagnostic canary")
    assert stream.getvalue() == ""
    assert formatter.calls == 0


class BufferedTransport:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass


def protocol_cycle(protocol, logs):
    import h11
    from uvicorn.protocols.http import h11_impl, httptools_impl
    from uvicorn.protocols.http.flow_control import FlowControl

    transport = BufferedTransport()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/canary",
        "raw_path": b"/canary",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"unit.test")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }
    arguments = dict(
        scope=scope,
        transport=transport,
        flow=FlowControl(transport),
        logger=logs["uvicorn.error"][0],
        access_logger=logs["uvicorn.access"][0],
        access_log=True,
        default_headers=[],
        message_event=asyncio.Event(),
        on_response=lambda: None,
    )
    if protocol == "h11":
        connection = h11.Connection(h11.SERVER)
        connection.receive_data(b"GET /canary HTTP/1.1\r\nHost: unit.test\r\n\r\n")
        assert isinstance(connection.next_event(), h11.Request)
        assert isinstance(connection.next_event(), h11.EndOfMessage)
        cycle = h11_impl.RequestResponseCycle(conn=connection, **arguments)
    else:
        cycle = httptools_impl.RequestResponseCycle(
            expect_100_continue=False, keep_alive=True, **arguments
        )
    return cycle, transport


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["h11", "httptools"])
@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("headers_sent", [False, True])
@pytest.mark.parametrize(
    "error_kind", ["application", "disconnect", "cancel", "group", "controlgroup"]
)
async def test_real_protocol_error_paths(
    monkeypatch, serving_logs, protocol, marked, headers_sent, error_kind
):
    from uvicorn.logging import TRACE_LOG_LEVEL
    from uvicorn.middleware.message_logger import MessageLoggerMiddleware

    errors = {
        "application": ValueError("private cycle canary"),
        "disconnect": ClientDisconnect("private cycle canary"),
        "cancel": asyncio.CancelledError("private cycle canary"),
        "group": ExceptionGroup("private cycle canary", [ValueError("private cycle leaf")]),
        "controlgroup": BaseExceptionGroup(
            "private cycle canary", [asyncio.CancelledError("private cycle leaf")]
        ),
    }
    original = errors[error_kind]
    cycle, transport = protocol_cycle(protocol, serving_logs)
    error, error_stream, error_formatter = serving_logs["uvicorn.error"]
    _access, access_stream, access_formatter = serving_logs["uvicorn.access"]
    verbose, verbose_stream, verbose_formatter = serving_logs["uvicorn.asgi"]
    observed = []
    emit = error.error

    def observe(message, *args, **kwargs):
        observed.append(kwargs["exc_info"])
        emit(message, *args, **kwargs)

    async def invoke(self, scope, receive, send):
        request_work.begin()
        if marked:
            assert request_work.mark_mirrored()
        if headers_sent:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise original

    monkeypatch.setattr(error, "error", observe)
    monkeypatch.setattr(FastAPI, "__call__", invoke)
    monkeypatch.setattr(verbose, "trace", getattr(verbose, "trace", None), raising=False)
    verbose.setLevel(TRACE_LOG_LEVEL)
    await cycle.run_asgi(MessageLoggerMiddleware(HTTPApplication()))
    assert len(observed) == 1
    if marked and error_kind == "application":
        assert isinstance(observed[0], MarkedRequestError)
        assert observed[0].__context__ is original
    else:
        assert observed[0] is original
    assert not request_work.http_mirror_marked()
    assert request_work.current() is None
    wire = b"".join(transport.writes)
    assert wire.startswith(b"HTTP/1.1 200" if headers_sent else b"HTTP/1.1 500")
    assert b"private cycle" not in wire
    assert transport.closed
    if marked:
        assert error_stream.getvalue() == access_stream.getvalue() == ""
        assert error_formatter.calls == access_formatter.calls == 0
    else:
        assert "private cycle canary" in error_stream.getvalue()
        assert error_formatter.calls == access_formatter.calls == 1
    assert verbose_stream.getvalue() == ""
    assert verbose_formatter.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["h11", "httptools"])
@pytest.mark.parametrize("marked", [False, True])
async def test_real_protocol_success_suppresses_completed_trace(
    monkeypatch, serving_logs, protocol, marked
):
    from uvicorn.logging import TRACE_LOG_LEVEL
    from uvicorn.middleware.message_logger import MessageLoggerMiddleware

    cycle, transport = protocol_cycle(protocol, serving_logs)
    verbose, verbose_stream, verbose_formatter = serving_logs["uvicorn.asgi"]

    async def invoke(self, scope, receive, send):
        request_work.begin()
        if marked:
            assert request_work.mark_mirrored()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"constant", "more_body": False})

    monkeypatch.setattr(FastAPI, "__call__", invoke)
    monkeypatch.setattr(verbose, "trace", getattr(verbose, "trace", None), raising=False)
    verbose.setLevel(TRACE_LOG_LEVEL)
    await cycle.run_asgi(MessageLoggerMiddleware(HTTPApplication()))
    assert cycle.response_complete
    assert b"constant" in b"".join(transport.writes)
    assert not request_work.http_mirror_marked()
    assert verbose_stream.getvalue() == ""
    assert verbose_formatter.calls == 0
    _access, stream, formatter = serving_logs["uvicorn.access"]
    assert formatter.calls == int(not marked)
    assert bool(stream.getvalue()) is not marked
