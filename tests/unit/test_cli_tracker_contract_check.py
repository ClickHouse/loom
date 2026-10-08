"""`loom tracker-contract-check --json`: stdout is the JSON document alone —
an unconfigured or unreachable source is reported on stderr and fails the
exit code — and the tracker client is closed on every path."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from server.cli import tracker


def test_json_stdout_parses_when_sources_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.tracker.app import contract
    from server.tracker.domain import sources
    from server.tracker.store.source import qa_intel

    closed: list[bool] = []

    async def _close() -> None:
        closed.append(True)

    async def _check(name: str, database: str) -> None:
        raise ConnectionError("refused")

    eps = {"down": SimpleNamespace(database="d1"), "nocred": SimpleNamespace(database="d2")}
    monkeypatch.setattr(sources, "endpoints", lambda: eps)
    monkeypatch.setattr(
        "server.tracker.domain.sources.credential_configured", lambda name: name == "down"
    )
    monkeypatch.setattr(qa_intel, "close", _close)
    monkeypatch.setattr(contract, "check", _check)

    args = argparse.Namespace(source="", namespace="", json=True)
    assert tracker._cmd_tracker_contract_check(args) == 1
    out, err = capsys.readouterr()
    assert json.loads(out) == {}
    assert "down (d1): UNAVAILABLE" in err and "nocred: not configured" in err
    assert closed == [True]
