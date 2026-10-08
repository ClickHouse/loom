"""The hook bounds derive from a measured round trip; the
doctor names every hand-set override and what it does."""

from __future__ import annotations

import json
from pathlib import Path

import server.cli.doctor_client as dc


def _texts(checks: list[tuple[str, str, str]]) -> list[str]:
    assert all(c[0] == "client:hook-override" and c[1] == dc.WARN for c in checks)
    return [c[2] for c in checks]


def test_no_overrides_no_lines(tmp_path: Path) -> None:
    assert dc.hook_override_check(tmp_path, {}) == []


def test_each_override_is_named_with_its_source_and_effect(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"LOOM_HOOK_TIMEOUT_MS": "8000", "LOOM_SHUNT_READ_CAP": "400"}})
    )
    out = _texts(dc.hook_override_check(tmp_path, {"LOOM_HOOK_EXIT_MS": "3000"}))
    assert len(out) == 3
    exit_line = next(t for t in out if t.startswith("LOOM_HOOK_EXIT_MS=3000"))
    assert "the environment" in exit_line and "redundant" in exit_line
    timeout_line = next(t for t in out if t.startswith("LOOM_HOOK_TIMEOUT_MS=8000"))
    assert "~/.claude/settings.json env" in timeout_line
    assert "hold a tool call for 8 s" in timeout_line and "round trip" in timeout_line
    cap_line = next(t for t in out if t.startswith("LOOM_SHUNT_READ_CAP=400"))
    assert "bench arm" in cap_line and "product default" in cap_line


def test_a_bound_below_the_floor_is_named_too(tmp_path: Path) -> None:
    out = _texts(dc.hook_override_check(tmp_path, {"LOOM_HOOK_TIMEOUT_MS": "500"}))
    assert len(out) == 1 and "below the built-in floor" in out[0]


def test_client_checks_include_the_override_lines() -> None:
    import inspect

    src = inspect.getsource(dc.client_checks)
    assert "hook_override_check(home, env)" in src
