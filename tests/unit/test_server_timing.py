"""Every /v1 answer says what it spent.

Diagnosis of a slow page started from trace queries every time; the
standard `Server-Timing` header puts handler time, ClickHouse time and
bytes read into the network panel the person reporting it already has open."""

from __future__ import annotations

from server.core import request_work
from server.transport.http import server_timing_header


def test_app_time_alone_when_no_work_bag_is_bound() -> None:
    request_work._current.set(None)
    assert server_timing_header(123) == "app;dur=123"


def test_clickhouse_time_and_bytes_ride_when_statements_ran() -> None:
    bag = request_work.begin()
    bag.ch_statements, bag.ch_elapsed_ms, bag.ch_read_bytes = 3, 45.67, 12_300_000
    h = server_timing_header(200)
    assert h.startswith('app;dur=200, ch;dur=45.7;desc="3 statements, 12.3 MB"')
    bag.ch_statements, bag.ch_read_bytes = 1, 512_000
    assert 'desc="1 statement, 512 kB"' in server_timing_header(7)
    request_work._current.set(None)


def test_a_bag_with_no_statements_adds_nothing() -> None:
    request_work.begin()
    assert server_timing_header(9) == "app;dur=9"
    request_work._current.set(None)


def test_the_endpoint_sets_server_timing() -> None:
    """The success path stamps `Server-Timing`, the one timing header."""
    import inspect

    from starlette.responses import Response

    from server.transport import http as http_mod

    # The timing pair and the mentor hint share one success-headers call; the
    # timing stamp rides the success path.
    assert "_success_headers(response, elapsed_ms" in inspect.getsource(http_mod._make_endpoint)
    assert "_stamp_timing(response, elapsed_ms)" in inspect.getsource(http_mod._success_headers)
    request_work._current.set(None)
    r = Response()
    http_mod._stamp_timing(r, 42)
    assert r.headers["Server-Timing"] == "app;dur=42" and "X-Server-Timing-Ms" not in r.headers
