"""The TypeScript compiler-tier worker
finds a bare `scip-typescript` on PATH; a configured path is kept as written."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from server.code.app.index import compiler_tier as w


def test_bare_name_is_looked_up_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "node" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "scip-typescript"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin")
    monkeypatch.chdir(tmp_path)  # a cwd-relative resolution would land here
    assert w.resolve_binary("scip-typescript") == exe.absolute()


def test_bare_name_missing_from_path_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.chdir(tmp_path)
    assert w.resolve_binary("scip-typescript") == (tmp_path / "scip-typescript").absolute()


def test_a_path_with_a_directory_is_kept_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    link = tmp_path / "node_modules" / ".bin" / "scip-typescript"
    assert w.resolve_binary(str(link)) == link.absolute()
    assert (
        w.resolve_binary("tools/scip-typescript")
        == (tmp_path / "tools" / "scip-typescript").absolute()
    )
