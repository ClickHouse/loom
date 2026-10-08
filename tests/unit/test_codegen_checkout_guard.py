"""A generator run by file path from one checkout never writes another
checkout's files: scripts.codegen.common refuses to write when the running
script belongs to a different checkout than the imported `scripts` package."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from scripts.codegen import common


def _checkout(root: Path) -> Path:
    for pkg in ("scripts", "server"):
        (root / pkg).mkdir(parents=True)
        (root / pkg / "__init__.py").write_text("")
    (root / "scripts" / "ops").mkdir()
    (root / "scripts" / "ops" / "tool.py").write_text("")
    return root


def _run_as(monkeypatch: pytest.MonkeyPatch, script: Path) -> None:
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__file__=str(script)))


def test_a_script_of_another_checkout_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _checkout(tmp_path / "other")
    _run_as(monkeypatch, other / "scripts" / "ops" / "tool.py")
    target = tmp_path / "out.txt"
    with pytest.raises(SystemExit, match="refusing to write"):
        common.write_atomic(target, b"x")
    assert not target.exists()


def test_a_script_of_this_checkout_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_as(monkeypatch, common.ROOT / "scripts" / "ops" / "dump_schema.py")
    target = tmp_path / "out.txt"
    assert common.write_atomic(target, b"x") and target.read_bytes() == b"x"


@pytest.mark.parametrize("entry", ["venv/bin/pytest", "other/.venv/bin/pytest", "other/tool.py"])
def test_an_entry_point_outside_a_checkouts_scripts_is_not_a_checkout_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    # pytest from a venv inside another checkout, or a file at its root, is no
    # repository script: the guard stays out of the way
    _checkout(tmp_path / "other")
    path = tmp_path / entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    _run_as(monkeypatch, path)
    assert common.running_checkout() is None
    common.refuse_foreign_checkout()


def test_no_main_file_is_no_checkout(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace())
    assert common.running_checkout() is None
