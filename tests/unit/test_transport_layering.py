"""Nothing below the transport imports it.

The dependency direction is transports → operations / handlers → services →
repositories. The pieces the lower layers need live below the
transport: the MCP toolset rosters
(server/operations/toolsets.py), the bearer check of the non-registry routes
(server/auth/bearer.py), the code-scope ContextVar (server/code/domain/scope.py),
the ledger row builder (server/observability/ledger.py), latency budgets
(server/observability/budgets.py) and the plane defaults (server/code/app/common.py).
Only the transports themselves, the app wiring (server/main.py) and the CLI
(an outer adapter) may import server.transport.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ALLOWED = ("server/transport/", "server/cli/", "server/main.py")


def _imports_transport(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("server.transport"):
            lines.append(node.lineno)
        elif isinstance(node, ast.Import) and any(
            a.name.startswith("server.transport") for a in node.names
        ):
            lines.append(node.lineno)
    return lines


def test_no_lower_layer_imports_the_transport() -> None:
    offenders = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(ALLOWED):
            continue
        for line in _imports_transport(ast.parse(path.read_text())):
            offenders.append(f"{rel}:{line}")
    assert offenders == []
