"""scip_ts_paths.py: the workspace package list the converter shares, and the tsconfig
patch and restore round trip the TypeScript harness runs around scip-typescript."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", _HARNESS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _workspace(root: Path) -> None:
    (root / "package.json").write_text(json.dumps({"name": "app", "workspaces": ["packages/*"]}))
    shared = root / "packages" / "shared"
    (shared / "src").mkdir(parents=True)
    (shared / "src" / "index.ts").write_text("export const x = 1;\n")
    (shared / "package.json").write_text(
        json.dumps({"name": "@acme/shared", "exports": {".": "./dist/index.js"}})
    )
    # the same name again, later in path order: the first one names the package
    (root / "packages" / "zz-copy").mkdir()
    (root / "packages" / "zz-copy" / "package.json").write_text(
        json.dumps({"name": "@acme/shared"})
    )
    # not a package: a list, no name, a dependency, a dot-directory
    (root / "packages" / "odd").mkdir()
    (root / "packages" / "odd" / "package.json").write_text("[1, 2]")
    (root / "packages" / "anon").mkdir()
    (root / "packages" / "anon" / "package.json").write_text("{}")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "package.json").write_text(json.dumps({"name": "dep"}))
    (root / ".cache" / "x").mkdir(parents=True)
    (root / ".cache" / "x" / "package.json").write_text(json.dumps({"name": "cached"}))
    web = root / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({"name": "web"}))
    (web / "tsconfig.json").write_text(
        '{\n  // comment\n  "compilerOptions": {"strict": true,},\n}\n'
    )


def test_the_converter_maps_packages_through_the_shared_list(tmp_path: Path) -> None:
    _workspace(tmp_path)
    paths = _load("scip_ts_paths")
    names = [
        (n, d.relative_to(tmp_path).as_posix()) for n, d, _ in paths.workspace_packages(tmp_path)
    ]
    assert names == [
        ("app", "."),
        ("@acme/shared", "packages/shared"),
        ("@acme/shared", "packages/zz-copy"),
        ("web", "web"),
    ]
    conv = _load("scip_to_delta")
    assert conv.ts_package_dirs(tmp_path) == {
        "app": "",
        "@acme/shared": "packages/shared",
        "web": "web",
    }
    assert conv.ts_package_dirs(None) == {} and conv.ts_package_dirs(tmp_path / "no") == {}


def _run(root: Path, *flags: str) -> str:
    out = subprocess.run(
        [sys.executable, str(_HARNESS / "scip_ts_paths.py"), str(root), *flags],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_patch_then_restore_puts_every_tsconfig_back(tmp_path: Path) -> None:
    _workspace(tmp_path)
    tsconfig = tmp_path / "web" / "tsconfig.json"
    original = tsconfig.read_bytes()

    assert "+1 paths" in _run(tmp_path, "--dry-run")
    assert tsconfig.read_bytes() == original
    assert not (tmp_path / "web" / "tsconfig.json.scip-bak").exists()

    _run(tmp_path)
    cfg = json.loads(tsconfig.read_text())
    assert cfg["compilerOptions"]["paths"] == {"@acme/shared": ["../packages/shared/src/index.ts"]}
    assert cfg["compilerOptions"]["strict"] is True
    assert (tmp_path / "web" / "tsconfig.json.scip-bak").read_bytes() == original

    assert "restored 1 tsconfig(s)" in _run(tmp_path, "--restore")
    assert tsconfig.read_bytes() == original
    assert not (tmp_path / "web" / "tsconfig.json.scip-bak").exists()
