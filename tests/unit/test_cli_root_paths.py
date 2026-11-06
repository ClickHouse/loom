"""The CLI's command modules find files beside the `server` package.

A command module that computes the root with `Path(__file__).parents[n]`
silently points at the wrong directory when it moves one level down; the
harness path then does not exist and the command exits 127. Every file the CLI
resolves against the root must exist in a checkout.
"""

from __future__ import annotations

import ast
from pathlib import Path

from server.core.paths import REPO_ROOT

CLI_DIR = Path(__file__).resolve().parents[2] / "server" / "cli"


def test_root_is_the_checkout() -> None:
    assert (REPO_ROOT / "server" / "cli" / "_paths.py").is_file()
    assert (REPO_ROOT / ".env.example").is_file()


def test_files_the_cli_resolves_against_the_root_exist() -> None:
    assert (REPO_ROOT / "scripts" / "scip_harness" / "run_delta.sh").is_file()
    assert (REPO_ROOT / "AGENTS.md").is_file()  # what `loom demo` ingests by default


def test_no_command_module_walks_parents_from_its_own_file() -> None:
    offenders = []
    for path in sorted(CLI_DIR.rglob("*.py")):
        if path.name == "_paths.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Path"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "__file__"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"read the root from server.cli._paths: {offenders}"
