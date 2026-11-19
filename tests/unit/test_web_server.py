"""The web tier's launcher (`server.core.web_server`): the uvicorn options it passes,
and the refusal of uvicorn's `UVICORN_<OPTION>` variables, which `uvicorn.run` would
drop without a word."""

from __future__ import annotations

from typing import Any

import click
import pytest
import uvicorn
from uvicorn.main import main as uvicorn_cli

from server.core import tls, web_server
from server.settings import settings

ARGV = [
    "--host",
    "0.0.0.0",
    "--port",
    "7777",
    "--workers",
    "1",
    "--loop",
    "asyncio",
    "--timeout-graceful-shutdown",
    "10",
    "--timeout-keep-alive",
    "5",
    "--forwarded-allow-ips=127.0.0.1",
]


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(settings, "tls_cert_file", "")
    monkeypatch.setattr(tls, "_served", False)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


def test_the_options_it_passes_are_the_ones_it_does_not_refuse(
    started: list[dict[str, Any]],
) -> None:
    assert web_server.main(ARGV) == 0
    assert set(started[0]) == web_server.PASSED
    options = {p.name for p in uvicorn_cli.params if isinstance(p, click.Option)}
    assert web_server.PASSED <= options


def test_a_variable_for_an_option_it_does_not_pass_stops_it_by_name(
    started: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("UVICORN_LOG_LEVEL", "debug")
    monkeypatch.setenv("UVICORN_SSL_CERTFILE", "/x")
    assert web_server.main(ARGV) == 2
    assert started == []
    assert "does not read UVICORN_LOG_LEVEL UVICORN_SSL_CERTFILE" in caplog.text


def test_other_uvicorn_names_do_not_stop_it(
    started: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The options it passes, the app it names, and the variables Kubernetes sets for a
    Service whose name starts with uvicorn: uvicorn's CLI read none of them either."""
    for name, value in {
        "UVICORN_PORT": "tcp://10.0.0.1:80",
        "UVICORN_WORKERS": "3",
        "UVICORN_APP": "other:app",
        "UVICORN_SERVICE_HOST": "10.0.0.1",
        "UVICORN_API_SERVICE_HOST": "10.0.0.2",
        "UVICORN_API_PORT_80_TCP_ADDR": "10.0.0.2",
    }.items():
        monkeypatch.setenv(name, value)
    assert web_server.main(ARGV) == 0
    assert started[0]["workers"] == 1


def test_every_refused_name_is_an_option_of_uvicorns_cli() -> None:
    options = {p.name for p in uvicorn_cli.params if isinstance(p, click.Option)}
    every = {f"UVICORN_{name.upper()}": "x" for name in options}
    refused = web_server.unread_uvicorn_variables(every)
    assert {f"UVICORN_{name.upper()}" for name in options - web_server.PASSED} == set(refused)
