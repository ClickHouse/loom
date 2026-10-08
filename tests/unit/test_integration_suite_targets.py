"""The integration suite reaches only the targets it was explicitly given.

On a developer host, localhost:8123 and :7777 can be a shared or production
store and server. So every ClickHouse client in tests/integration is built by
`_resources.ch_client_from_env` (which skips without LOOM_CH_HOST), every
Loom client names its base URL, and no environment read falls back to a
default host.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parents[1] / "integration"
CLIENT_FACTORY = "_resources.py"
_DRIVER_CONSTRUCTORS = {"get_client", "get_async_client"}
_TARGET_ENV = {"LOOM_CH_HOST", "LOOM_BASE_URL"}
# An in-process ASGI transport's placeholder origin; it opens no socket.
_ASGI_ORIGIN = "http://test"


def _modules() -> list[Path]:
    return sorted(SUITE.glob("*.py"))


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return f"{_name(func.value)}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "clickhouse_connect":
            imported = {a.name for a in node.names} & _DRIVER_CONSTRUCTORS
            if imported and path.name != CLIENT_FACTORY:
                out.append(f"{path.name}:{node.lineno} imports {sorted(imported)}")
    for call in _calls(tree):
        name = _name(call.func)
        if name.startswith("clickhouse_connect.") and name.split(".")[-1] in _DRIVER_CONSTRUCTORS:
            if path.name != CLIENT_FACTORY:
                out.append(f"{path.name}:{call.lineno} builds a ClickHouse client directly")
        if name.endswith("environ.get") and len(call.args) >= 2:
            key = call.args[0]
            if isinstance(key, ast.Constant) and key.value in _TARGET_ENV:
                out.append(f"{path.name}:{call.lineno} defaults {key.value}")
        if name == "Agent" and not any(k.arg == "base_url" for k in call.keywords):
            out.append(f"{path.name}:{call.lineno} builds an Agent without base_url")
        for kw in call.keywords:
            if kw.arg == "base_url" and isinstance(kw.value, ast.Constant):
                if kw.value.value != _ASGI_ORIGIN:
                    out.append(f"{path.name}:{call.lineno} hard-codes base_url={kw.value.value!r}")
    return out


_REPO = SUITE.parents[1]
_DEV_CLI = re.compile(r'"dev"\s*/\s*"([\w.]+\.py)"')
_READS_CLICKHOUSE = re.compile(r"_ch_target|LOOM_CH_|server\.ch\b|clickhouse_connect")


def _cli_violations(path: Path) -> list[str]:
    """A dev/ CLI run as a subprocess reads LOOM_CH_* on its own, past the
    in-process guard, so its test must require an explicit target."""
    src = path.read_text()
    out = []
    for cli in sorted(set(_DEV_CLI.findall(src))):
        script = _REPO / "dev" / cli
        if script.exists() and _READS_CLICKHOUSE.search(script.read_text()):
            if "ch_target_required" not in src:
                out.append(f"{path.name} runs dev/{cli} without ch_target_required")
    return out


def test_the_suite_has_modules_to_check() -> None:
    assert len(_modules()) > 50, "tests/integration moved; point SUITE at it"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_integration_module_reaches_only_explicit_targets(path: Path) -> None:
    found = _violations(path) + _cli_violations(path)
    assert not found, found


def test_the_gate_catches_each_implicit_target(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import os, clickhouse_connect\n"
        "from clickhouse_connect import get_client\n"
        "c = clickhouse_connect.get_client(host='localhost')\n"
        "h = os.environ.get('LOOM_CH_HOST', 'localhost')\n"
        "a = Agent(org='o', namespace='n', agent='a')\n"
        "b = Agent(org='o', namespace='n', agent='a', base_url='http://localhost:7777')\n"
        "t = httpx.Client(base_url='http://test')\n"
    )
    found = _violations(probe)
    assert [v.split(" ", 1)[1] for v in found] == [
        "imports ['get_client']",
        "builds a ClickHouse client directly",
        "defaults LOOM_CH_HOST",
        "builds an Agent without base_url",
        "hard-codes base_url='http://localhost:7777'",
    ], found


def test_the_gate_catches_a_clickhouse_cli_without_the_target_fixture(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text('CLI = ROOT / "dev" / "backup_loom.py"\n')
    assert _cli_violations(probe) == [
        "test_probe.py runs dev/backup_loom.py without ch_target_required"
    ]
