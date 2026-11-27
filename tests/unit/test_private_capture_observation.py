"""Exercise the native-log test's capture boundary without a database."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.core import ch, diagnostics
from server.settings import settings
from tests.integration import test_private_database_log_boundary as boundary


class NativeBoundaryReached(Exception):
    """The capture assertions passed; the real database checks start here."""


class CaptureDriver:
    def __init__(self, background, *, leak=None):
        self.background = background
        self.leak = leak
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.disconnected = False

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def public_work(self):
        await self.ready.wait()
        try:
            assert not diagnostics.codes_only()
            if self.background:
                ch.set_query_tag("test.unrelated_public_capture")
                ch.statement_settings("SELECT 1 AS unrelated_public_control")
        finally:
            self.finished.set()

    async def raw_bytes(self, sql, *, settings):
        assert diagnostics.codes_only()
        ch.statement_settings(sql, settings, cls="read")
        self.ready.set()
        await self.finished.wait()
        if self.leak == "owner":
            self.force_private_capture(sql)
        elif self.leak == "child":
            await asyncio.create_task(self.private_child(sql))
        return b"synthetic-private-code\n"

    @staticmethod
    def force_private_capture(sql):
        assert diagnostics.codes_only()
        ch.capture_statement(sql, settings.ch_capture_statements)

    async def private_child(self, sql):
        self.force_private_capture(sql)

    async def query_one(self, sql):
        assert not diagnostics.codes_only()
        ch.statement_settings(sql)
        return {"public_control": 1}

    async def execute(self, sql):
        assert sql == "SYSTEM FLUSH LOGS"
        raise NativeBoundaryReached


def captured_sql(path):
    return [json.loads(line)["sql"] for line in path.read_text().splitlines()]


async def run_boundary(monkeypatch, tmp_path, *, background, leak=None):
    driver = CaptureDriver(background, leak=leak)
    monkeypatch.setenv("LOOM_CH_HOST", "unit-capture-only")
    monkeypatch.setattr(ch, "get_client", lambda: driver)
    # This task predates the private request and must not inherit its scope.
    public = asyncio.create_task(driver.public_work())
    tag = ch.query_tag_var.set("")
    try:
        await boundary.test_native_private_query_log_is_inaccessible_to_sql_and_analyst(
            "http://unused.invalid", lambda **_: "unit-token", monkeypatch, tmp_path
        )
    finally:
        driver.ready.set()
        await public
        ch.query_tag_var.reset(tag)
        assert driver.disconnected


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("original_sink", [False, True])
async def test_capture_observes_only_the_logical_request(
    monkeypatch, tmp_path, background, original_sink
):
    original = tmp_path / "original.jsonl"
    monkeypatch.setattr(settings, "ch_capture_statements", str(original) if original_sink else "")
    with pytest.raises(NativeBoundaryReached):
        await run_boundary(monkeypatch, tmp_path, background=background)
    assert captured_sql(tmp_path / "statements.jsonl") == ["SELECT 1 AS public_control"]
    if original_sink and background:
        assert captured_sql(original) == ["SELECT 1 AS unrelated_public_control"]
    else:
        assert not original.exists()


@pytest.mark.parametrize("leak", ["owner", "child"])
async def test_private_capture_from_owner_or_child_still_fails(monkeypatch, tmp_path, leak):
    monkeypatch.setattr(settings, "ch_capture_statements", "")
    with pytest.raises(AssertionError):
        await run_boundary(monkeypatch, tmp_path, background=True, leak=leak)
    assert "SELECT 'synthetic-private-code' FORMAT TabSeparated" in captured_sql(
        tmp_path / "statements.jsonl"
    )
