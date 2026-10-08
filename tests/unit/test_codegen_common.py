"""The write-or-check protocol every generator shares: `--check` never writes,
a write is atomic and happens only when the bytes change."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.codegen.common import GeneratedFile, generator_main, write_atomic


def _target(path: Path) -> GeneratedFile:
    return GeneratedFile(path, "python gen.py")


def test_check_reports_stale_and_writes_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "out" / "gen.txt"
    assert _target(missing).sync("a\n", check=True) == 1
    assert not missing.parent.exists()

    stale = tmp_path / "gen.txt"
    stale.write_text("old\n")
    assert _target(stale).sync("new\n", check=True) == 1
    assert stale.read_text() == "old\n"


def test_check_passes_on_a_current_file(tmp_path: Path) -> None:
    f = tmp_path / "gen.txt"
    f.write_text("a\n")
    assert _target(f).sync("a\n", check=True) == 0


def test_check_compares_normalised_text(tmp_path: Path) -> None:
    f = tmp_path / "gen.txt"
    f.write_text("A\n")
    target = GeneratedFile(f, "python gen.py", normalise=str.lower)
    assert target.sync("a\n", check=True) == 0


def test_write_creates_the_file_and_its_directory(tmp_path: Path) -> None:
    f = tmp_path / "out" / "gen.txt"
    assert _target(f).sync("a\n", check=False) == 0
    assert f.read_text() == "a\n"


def test_write_leaves_an_unchanged_file_untouched(tmp_path: Path) -> None:
    f = tmp_path / "gen.txt"
    f.write_text("a\n")
    os.utime(f, (1, 1))
    before = f.stat()
    assert _target(f).sync("a\n", check=False) == 0
    after = f.stat()
    assert (after.st_ino, after.st_mtime) == (before.st_ino, before.st_mtime)


def test_write_atomic_replaces_keeps_mode_and_leaves_no_temp(tmp_path: Path) -> None:
    f = tmp_path / "gen.sh"
    f.write_bytes(b"old")
    f.chmod(0o755)
    assert write_atomic(f, b"new") is True
    assert f.read_bytes() == b"new"
    assert f.stat().st_mode & 0o777 == 0o755
    assert sorted(p.name for p in tmp_path.iterdir()) == ["gen.sh"]
    assert write_atomic(f, b"new") is False


def test_generator_main_check_and_out(tmp_path: Path) -> None:
    default = tmp_path / "default.txt"
    other = tmp_path / "other.txt"
    target = _target(default)
    assert generator_main(None, target, lambda: "x\n", ["--check"]) == 1
    assert generator_main(None, target, lambda: "x\n", ["--out", str(other)]) == 0
    assert other.read_text() == "x\n"
    assert not default.exists()


def test_plugin_sync_copies_changed_files_and_drops_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.codegen import agent_plugin_sync as mod

    src, dst = tmp_path / "claude", tmp_path / "codex"
    (src / "hooks").mkdir(parents=True)
    (src / "hooks" / "a.mjs").write_text("a")
    (src / "hooks" / "b.mjs").write_text("b")
    (dst / "hooks").mkdir(parents=True)
    (dst / "hooks" / "a.mjs").write_text("a")
    (dst / "hooks" / "gone.mjs").write_text("x")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "SOURCE", src)
    monkeypatch.setattr(mod, "TARGET", dst)

    assert sorted(mod.check()) == [
        "missing: codex/hooks/b.mjs",
        "orphan (no source): codex/hooks/gone.mjs",
    ]
    assert (dst / "hooks" / "b.mjs").exists() is False  # check wrote nothing
    assert mod.sync() == 2
    assert sorted(p.name for p in (dst / "hooks").iterdir()) == ["a.mjs", "b.mjs"]
    assert mod.check() == []
    assert mod.sync() == 0


def test_plugin_sync_mirrors_the_executable_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.codegen import agent_plugin_sync as mod

    src, dst = tmp_path / "claude", tmp_path / "codex"
    for root in (src, dst):
        (root / "hooks").mkdir(parents=True)
        (root / "hooks" / "run.mjs").write_text("x")
        (root / "hooks" / "lib.mjs").write_text("y")
    (src / "hooks" / "run.mjs").chmod(0o755)
    (src / "hooks" / "lib.mjs").chmod(0o644)
    (dst / "hooks" / "run.mjs").chmod(0o644)
    (dst / "hooks" / "lib.mjs").chmod(0o755)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "SOURCE", src)
    monkeypatch.setattr(mod, "TARGET", dst)

    assert sorted(mod.check()) == [
        "mode differs (executable bit): codex/hooks/lib.mjs",
        "mode differs (executable bit): codex/hooks/run.mjs",
    ]
    assert (dst / "hooks" / "run.mjs").stat().st_mode & 0o777 == 0o644  # check wrote nothing
    assert mod.sync() == 2
    assert (dst / "hooks" / "run.mjs").stat().st_mode & 0o777 == 0o755
    assert (dst / "hooks" / "lib.mjs").stat().st_mode & 0o777 == 0o644
    assert mod.check() == []
    assert mod.sync() == 0


def test_the_library_method_table_keeps_its_head_and_lists_sorted_names() -> None:
    """The table depends on the interpreter, so CI never runs its `--check`;
    the generator's shape is pinned here instead."""
    import ast

    from scripts.codegen import gen_py_library_methods as gen

    out = gen.build()
    head = gen.TARGET.path.read_text(encoding="utf-8").partition(gen._TABLE_HEAD)[0]
    assert out.startswith(head.split("review the diff;", 1)[0])
    table = out.partition(gen._TABLE_HEAD)[2]
    names = [ast.literal_eval(line.strip().rstrip(",")) for line in table.splitlines()[1:-1]]
    assert names == sorted(set(names)) and {"append", "split", "items"} <= set(names)
    assert all(gen._METHOD_RE.match(n) for n in names)
    ast.parse(out)
