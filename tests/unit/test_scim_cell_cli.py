"""The actual command entry generates only a local named random secret."""

from __future__ import annotations

import builtins
import io
import re
import socket

import httpx
import pytest

from server.cli import app
from server.core import pg
from server.core.ch import CHClient
from server.insights.app import provider_barrier, provider_command


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline token generation attempted a runtime dependency")

    monkeypatch.setenv("LOOM_ENV_FILE", "")
    monkeypatch.setattr(CHClient, "connect", forbidden)
    monkeypatch.setattr(pg, "open_pool", forbidden)
    monkeypatch.setattr(provider_command, "model_command", forbidden)
    monkeypatch.setattr(provider_barrier, "enroll_before_migrations", forbidden)
    monkeypatch.setattr(provider_barrier, "register_process", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    # Imports may read source/metadata. No file write belongs to this command.
    opened = builtins.open

    def read_only(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            forbidden()
        return opened(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", read_only)
    monkeypatch.setattr(io, "open", read_only)


@pytest.mark.parametrize("name", ["a", "a-b", "z" * 32])
def test_actual_command_is_offline_and_draws_distinct_native_random_secrets(offline, capsys, name):
    outputs = []
    for _ in range(2):
        assert app.main(["cell", "scim-token", "new", "--cell", name]) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert re.fullmatch(r"lsc_" + name + r"_[0-9a-f]{64}\n", captured.out)
        outputs.append(captured.out)
    assert outputs[0] != outputs[1]


@pytest.mark.parametrize("name", ["", "a_b", "A", "z" * 33, "a\n", "é"])
def test_invalid_names_refuse_without_a_secret_or_runtime_dependency(offline, capsys, name):
    assert app.main(["cell", "scim-token", "new", "--cell", name]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "cell" in captured.err
