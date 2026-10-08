"""The repository formatter stays inside this checkout and names prerequisites."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_agent_worktrees_are_outside_the_parent_prettier_run() -> None:
    ignored = (ROOT / ".prettierignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/worktrees/" in ignored
    assert ".codex/worktrees/" in ignored


def test_make_format_explains_the_unbundled_shell_formatter() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "command -v $(SHFMT)" in makefile
    assert "missing shfmt" in makefile
