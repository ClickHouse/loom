"""The fixed creator probe accepts only the published HTTP query response."""

import http.client
import sys

import pytest

from scripts.gates import cell_trigger_acceptance as creator


@pytest.mark.parametrize(
    ("status", "body", "failure", "expected"),
    [
        (200, b"1\n", None, True),
        (200, b"1\nx", None, False),
        (200, b"1", None, False),
        (200, b"", None, False),
        (500, b"1\n", None, False),
        (200, b"1\n", "disconnect", False),
        (200, b"1\n", "socket", False),
    ],
)
def test_fixed_http_probe_checks_response_and_closes_connection(
    monkeypatch, capsys, status, body, failure, expected
):
    calls = []

    class Response:
        def __init__(self):
            self.status = status

        def read(self, amount):
            assert amount == 3
            calls.append("read")
            return body

    class Connection:
        def request(self, method, path, *, body, headers):
            assert (method, path, body) == ("POST", "/", "SELECT 1")
            assert headers == {"X-ClickHouse-User": "loom", "X-ClickHouse-Key": "loomdev"}
            calls.append("request")
            if failure == "socket":
                raise OSError("not listening")

        def getresponse(self):
            calls.append("response")
            if failure == "disconnect":
                raise http.client.RemoteDisconnected("bootstrap stopped")
            return Response()

        def close(self):
            calls.append("close")

    def connect(host, port, *, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 18000, 2)
        return Connection()

    monkeypatch.setattr(http.client, "HTTPConnection", connect)
    monkeypatch.setattr(sys, "argv", ["probe", "18000"])
    exec(creator._CLICKHOUSE_READY, {})
    assert capsys.readouterr().out == ("1\n" if expected else "")
    assert calls[-1] == "close"
    assert calls.count("close") == 1
    assert calls.count("read") == (1 if status == 200 and failure is None else 0)


def test_readiness_uses_existing_bounded_creator_command(tmp_path):
    class RecordingStores(creator.Stores):
        def command(self, *argv, timeout=120, cleanup=False):
            self.recorded = (argv, timeout, cleanup)
            return "1"

    stores = RecordingStores(tmp_path, tmp_path, "fixture", {"run_id": "123"})
    assert stores._clickhouse_ready(18000)
    assert stores.recorded == (
        (sys.executable, "-I", "-S", "-c", creator._CLICKHOUSE_READY, "18000"),
        2,
        False,
    )
