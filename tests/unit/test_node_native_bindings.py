"""Locked tooling never delegates a missing native package to an installer download."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/code-toolchains/bind-native.cjs"
HOST = {"platform": "linux", "arch": "x64", "musl": False}
CASES = {
    "bun": ("bun", "@oven/bun-linux-x64-baseline", "bin/bun", ("bin/bun.exe", "bin/bunx.exe")),
    "pnpm": ("pnpm", "@pnpm/exe.linux-x64", "pnpm", ("pnpm", "pn", "pnpx", "pnx")),
    "claude": (
        "@anthropic-ai/claude-code",
        "@anthropic-ai/claude-code-linux-x64",
        "claude",
        ("bin/claude.exe",),
    ),
}
NATIVE = b"native binary from the locked optional package\n"
PLACEHOLDER = b"unbound placeholder\n"


def make_project(tmp_path: Path, tools: tuple[str, ...]) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    manifest: dict = {"dependencies": {}, "optionalDependencies": {}}
    lock: dict = {"packages": {}}
    for tool in tools:
        wrapper, native, binary, targets = CASES[tool]
        manifest["dependencies"][wrapper] = "1.2.3"
        manifest["optionalDependencies"][native] = "1.2.3"
        wrapper_dir = project / "node_modules" / wrapper
        native_dir = project / "node_modules" / native
        wrapper_dir.mkdir(parents=True)
        native_dir.mkdir(parents=True)
        (wrapper_dir / "package.json").write_text(json.dumps({"name": wrapper, "version": "1.2.3"}))
        (native_dir / "package.json").write_text(json.dumps({"name": native, "version": "1.2.3"}))
        source = native_dir / binary
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(NATIVE)
        for target in targets:
            dest = wrapper_dir / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(PLACEHOLDER)
        lock["packages"][f"node_modules/{native}"] = {
            "version": "1.2.3",
            "integrity": "sha512-" + "A" * 86 + "==",
            "resolved": f"https://registry.npmjs.org/{native}/-/native.tgz",
        }
    (project / "package.json").write_text(json.dumps(manifest))
    (project / "package-lock.json").write_text(json.dumps(lock))
    return project


def bind(
    project: Path,
    tools: tuple[str, ...],
    tmp_path: Path,
    host: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    guard = tmp_path / "network-guard.cjs"
    guard.write_text(
        "const fs = require('node:fs');\n"
        "function blocked() { fs.appendFileSync(process.env.BIND_LEAK_LOG, 'leak\\n'); throw Error('network or process launch'); }\n"
        "globalThis.fetch = blocked;\n"
        "for (const name of ['node:http', 'node:https']) { const api = require(name); api.get = api.request = blocked; }\n"
        "const net = require('node:net'); net.connect = net.createConnection = blocked;\n"
        "const cp = require('node:child_process');\n"
        "for (const name of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) cp[name] = blocked;\n"
    )
    return subprocess.run(
        [
            shutil.which("node") or "node",
            "--require",
            str(guard),
            "-e",
            "require(process.argv[1]).bindNative(process.argv[2], JSON.parse(process.argv[3]), JSON.parse(process.argv[4]));",
            str(HELPER),
            str(project),
            json.dumps(tools),
            json.dumps(HOST if host is None else host),
        ],
        env={**os.environ, "BIND_LEAK_LOG": str(tmp_path / "leaks")},
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("tool", CASES)
def test_native_binding_uses_the_locked_optional_package(tmp_path: Path, tool: str) -> None:
    project = make_project(tmp_path, (tool,))
    result = bind(project, (tool,), tmp_path)
    assert result.returncode == 0, result.stderr
    wrapper, _, _, targets = CASES[tool]
    assert all(
        (project / "node_modules" / wrapper / target).read_bytes() == NATIVE for target in targets
    )
    assert not (tmp_path / "leaks").exists()


@pytest.mark.parametrize("tool", CASES)
@pytest.mark.parametrize("missing", ["package", "binary"])
def test_missing_native_fails_without_a_download(tmp_path: Path, tool: str, missing: str) -> None:
    project = make_project(tmp_path, (tool,))
    wrapper, native, binary, targets = CASES[tool]
    native_dir = project / "node_modules" / native
    if missing == "package":
        shutil.rmtree(native_dir)
    else:
        (native_dir / binary).unlink()
    result = bind(project, (tool,), tmp_path)
    assert result.returncode != 0
    assert all(
        (project / "node_modules" / wrapper / target).read_bytes() == PLACEHOLDER
        for target in targets
    )
    assert not (tmp_path / "leaks").exists()


def test_all_required_packages_are_checked_before_binding(tmp_path: Path) -> None:
    project = make_project(tmp_path, ("bun", "pnpm"))
    (project / "node_modules" / CASES["pnpm"][1] / "pnpm").unlink()
    result = bind(project, ("bun", "pnpm"), tmp_path)
    assert result.returncode != 0
    assert (project / "node_modules/bun/bin/bun.exe").read_bytes() == PLACEHOLDER
    assert not (tmp_path / "leaks").exists()


def test_native_binary_cannot_escape_its_locked_package(tmp_path: Path) -> None:
    project = make_project(tmp_path, ("bun",))
    source = project / "node_modules" / CASES["bun"][1] / "bin/bun"
    source.unlink()
    outside = tmp_path / "unlocked"
    outside.write_bytes(NATIVE)
    source.symlink_to(outside)
    result = bind(project, ("bun",), tmp_path)
    assert result.returncode != 0
    assert (project / "node_modules/bun/bin/bun.exe").read_bytes() == PLACEHOLDER
    assert not (tmp_path / "leaks").exists()


def test_windows_package_manager_targets_native_executables(tmp_path: Path) -> None:
    project = make_project(tmp_path, ("pnpm",))
    old_name = CASES["pnpm"][1]
    native = "@pnpm/exe.win32-x64"
    native_dir = project / "node_modules" / native
    (project / "node_modules" / old_name).rename(native_dir)
    (native_dir / "pnpm").rename(native_dir / "pnpm.exe")
    (native_dir / "package.json").write_text(json.dumps({"name": native, "version": "1.2.3"}))
    file = project / "package.json"
    manifest = json.loads(file.read_text())
    manifest["optionalDependencies"] = {native: "1.2.3"}
    file.write_text(json.dumps(manifest))
    file = project / "package-lock.json"
    lock = json.loads(file.read_text())
    lock["packages"][f"node_modules/{native}"] = lock["packages"].pop(f"node_modules/{old_name}")
    file.write_text(json.dumps(lock))
    result = bind(project, ("pnpm",), tmp_path, {"platform": "win32", "arch": "x64"})
    assert result.returncode == 0, result.stderr
    wrapper = project / "node_modules/pnpm"
    targets = {name: name + ".exe" for name in ("pnpm", "pn", "pnpx", "pnx")}
    assert json.loads((wrapper / "package.json").read_text())["bin"] == targets
    assert all((wrapper / target).read_bytes() == NATIVE for target in targets.values())
    assert not (tmp_path / "leaks").exists()


def test_the_leak_control_intercepts_a_nested_package_install(tmp_path: Path) -> None:
    project = make_project(tmp_path, ("bun",))
    assert bind(project, ("bun",), tmp_path).returncode == 0
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            "--require",
            str(tmp_path / "network-guard.cjs"),
            "-e",
            "try { require('node:child_process').spawnSync('npm', ['install', 'unlocked']); } catch {}",
        ],
        env={**os.environ, "BIND_LEAK_LOG": str(tmp_path / "leaks")},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert (tmp_path / "leaks").read_text() == "leak\n"


def test_tool_projects_never_run_vendor_lifecycle_scripts() -> None:
    paths = [
        "scripts/install-code-toolchains.sh",
        ".devcontainer/start-loom.sh",
        "examples/course-starter/.devcontainer/start-loom.sh",
        ".github/workflows/plugin-platforms.yml",
        ".github/workflows/n8n-publish.yml",
    ]
    for filename in paths:
        commands = [
            shlex.split(line.strip())
            for line in (ROOT / filename).read_text().splitlines()
            if line.strip().startswith("npm ci ")
        ]
        assert commands, filename
        assert all("--ignore-scripts" in command for command in commands), filename
