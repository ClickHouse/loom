"""The Go compiler-tier worker reads its window from a real checkout: the
module directories (every go.mod) and the paths the window changed. Both
name files verbatim, so a module under a non-ASCII or spaced directory is
indexed, and a base the checkout lacks is BaseUnknown (the next run is
full), never an empty window."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from server.code.app.index.compiler_tier import BaseUnknown
from server.code.workers import scip_go_delta as w


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _no_user_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str, str]:
    co = tmp_path / "co"
    co.mkdir()
    _git(co, "init", "-q", "-b", "main")
    _git(co, "config", "user.email", "t@example.invalid")
    _git(co, "config", "user.name", "t")
    for d in ("", "svc/api", "svc/café", "tools/my tool"):
        (co / d).mkdir(parents=True, exist_ok=True)
        (co / d / "go.mod").write_text("module m\n")
    (co / "svc/api/main.go").write_text("package main\n")
    _git(co, "add", ".")
    _git(co, "commit", "-q", "-m", "base")
    base = _git(co, "rev-parse", "HEAD")
    (co / "svc/café/handler.go").write_text("package café\n")
    (co / "tools/my tool/run.go").write_text("package main\n")
    (co / "svc/api/main.go").unlink()
    _git(co, "add", "-A")
    _git(co, "commit", "-q", "-m", "window")
    return co, base, _git(co, "rev-parse", "HEAD")


def test_module_dirs_name_every_go_mod_verbatim(checkout: tuple[Path, str, str]) -> None:
    co, _base, _head = checkout
    assert asyncio.run(w._module_dirs(co)) == ["", "svc/api", "svc/café", "tools/my tool"]


def test_the_window_names_its_changed_paths_verbatim(checkout: tuple[Path, str, str]) -> None:
    co, base, head = checkout
    changed = asyncio.run(w._changed_paths(co, base, head))
    assert sorted(changed) == ["svc/api/main.go", "svc/café/handler.go", "tools/my tool/run.go"]
    modules = w.modules_for_window(changed, asyncio.run(w._module_dirs(co)))
    assert sorted(modules) == ["svc/api", "svc/café", "tools/my tool"]


def test_a_base_the_checkout_lacks_is_base_unknown(checkout: tuple[Path, str, str]) -> None:
    co, _base, head = checkout
    with pytest.raises(BaseUnknown):
        asyncio.run(w._changed_paths(co, "0" * 40, head))


def test_an_option_shaped_base_is_a_revision_not_an_option(
    checkout: tuple[Path, str, str], tmp_path: Path
) -> None:
    co, _base, head = checkout
    target = tmp_path / "written"
    with pytest.raises(BaseUnknown):
        asyncio.run(w._changed_paths(co, f"--output={target}", head))
    assert not target.exists()
