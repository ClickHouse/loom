"""LOOM_CH_CAPTURE_STATEMENTS appends every executed statement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.core import ch as ch_mod
from server.settings import settings


def test_capture_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_capture_statements", "")
    out = ch_mod.statement_settings("SELECT 1 FROM loom.memory", None, cls="read")
    assert isinstance(out, dict)
    assert not list(tmp_path.iterdir())


def test_capture_appends_digest_and_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "statements.jsonl"
    monkeypatch.setattr(settings, "ch_capture_statements", str(target))
    ch_mod.statement_settings("SELECT 1 FROM loom.memory WHERE org = 'a'", None, cls="read")
    ch_mod.statement_settings("SELECT 1 FROM loom.memory WHERE org = 'b'", None, cls="read")
    ch_mod.statement_settings("ALTER TABLE loom.memory DELETE WHERE org = 'c'", None, cls="write")
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(rows) == 3
    assert rows[0]["sql"].endswith("org = 'a'") and rows[0]["digest"].startswith("sql:")
    # Two statements that differ only in a literal share a shape.
    assert rows[0]["digest"].split(" ")[0] == rows[1]["digest"].split(" ")[0]
    assert rows[2]["digest"].split(" ")[0] != rows[0]["digest"].split(" ")[0]


def test_capture_write_failure_does_not_fail_the_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ch_capture_statements", str(tmp_path / "no-such-dir" / "x.jsonl"))
    out = ch_mod.statement_settings("SELECT 1", None, cls="read")
    assert isinstance(out, dict)
