"""Retired native account commands cannot send requests or launch consent."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from loom import cli


@pytest.mark.parametrize("provider", ["google", "gdrive", "notion", "linear"])
def test_native_connect_is_rejected_before_any_http_or_browser(
    monkeypatch: pytest.MonkeyPatch, provider: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Retired native account setup attempted external work")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr("webbrowser.open", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(["connect", provider])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert "connect" not in cli.COMMANDS


def test_server_cli_does_not_delegate_retired_native_connect() -> None:
    from server.cli.login import SDK_COMMANDS

    assert "connect" not in SDK_COMMANDS
