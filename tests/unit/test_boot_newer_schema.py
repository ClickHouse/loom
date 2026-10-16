"""An older build booting on a newer schema says so at WARNING."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from server import boot
from server.core.schema import status


def _status(ledger_only: list[str] | None) -> Any:
    async def fake(**_: Any) -> dict[str, Any]:
        return {"pending": [], "ledger_only": ledger_only, "applied": 28}

    return fake


def test_ledger_only_files_are_logged_at_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(status, "schema_status", _status(["029_new.sql", "030_newer.sql"]))
    with caplog.at_level(logging.WARNING, logger="server.boot"):
        assert asyncio.run(boot.warn_newer_schema()) == ["029_new.sql", "030_newer.sql"]
    [rec] = [r for r in caplog.records if r.name == "server.boot"]
    assert rec.levelno == logging.WARNING
    assert "029_new.sql, 030_newer.sql" in rec.getMessage()


@pytest.mark.parametrize("ledger_only", [[], None])
def test_same_or_unreadable_schema_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, ledger_only: Any
) -> None:
    monkeypatch.setattr(status, "schema_status", _status(ledger_only))
    with caplog.at_level(logging.WARNING, logger="server.boot"):
        assert asyncio.run(boot.warn_newer_schema()) == []
    assert not [r for r in caplog.records if r.name == "server.boot"]
