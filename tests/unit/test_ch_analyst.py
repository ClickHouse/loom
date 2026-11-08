"""The client's analyst path: a statement inside `as_analyst` logs in as the
analyst user with its own settings, and a statement inside `uncaptured`, one
that carries a password or a key, never reaches the capture file."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from server.core import ch
from server.settings import settings


class _Driver:
    """The driver under CHClient: records each call's keyword arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def query(self, _sql: str, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        column_type = type("T", (), {"name": "UInt64"})()
        return type(
            "R",
            (),
            {
                "result_rows": [(1,)],
                "column_names": ["n"],
                "column_types": [column_type],
                "summary": {},
            },
        )()


def _client(driver: _Driver) -> ch.CHClient:
    client = ch.CHClient.__new__(ch.CHClient)
    client._client = driver  # type: ignore[assignment]
    return client


@pytest.mark.asyncio
async def test_a_read_inside_as_analyst_logs_in_as_the_analyst_with_its_settings() -> None:
    driver = _Driver()
    with ch.as_analyst("loom_analyst", "pw", {"SQL_loom_org": "acme", "query_id": "q-1"}):
        names, types, rows = await _client(driver).query_columns("SELECT count() AS n")
    assert (names, types, rows) == (["n"], ["UInt64"], [(1,)])
    (call,) = driver.calls
    auth = call["transport_settings"]["Authorization"]
    assert base64.b64decode(auth.removeprefix("Basic ")).decode() == "loom_analyst:pw"
    assert call["settings"]["SQL_loom_org"] == "acme"
    assert call["settings"]["query_id"] == "q-1"
    # The analyst profile pins the time cap: the class deadline is not sent.
    assert "max_execution_time" not in call["settings"]


def test_a_statement_inside_uncaptured_stays_out_of_the_capture_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "statements.jsonl"
    monkeypatch.setattr(settings, "ch_capture_statements", str(target))
    with ch.uncaptured():
        ch.statement_settings("CREATE USER loom_analyst IDENTIFIED BY 'secret-pw'", None)
    ch.statement_settings("SELECT 1", None, cls="read")
    captured = target.read_text()
    assert "secret-pw" not in captured
    assert "SELECT 1" in captured
