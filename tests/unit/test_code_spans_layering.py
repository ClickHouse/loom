"""The code plane's span helpers live in server.code.app.spans, and code
imports them from there rather than through the handlers' common module."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SPAN_NAMES = {
    "CODE_SPAN_KEYS",
    "StageWall",
    "code_span",
    "current_otel_ids",
    "current_stage_ms",
    "format_otel_ids",
    "set_code_span_attributes",
    "span_attrs",
    "stage_of",
    "start_stage_wall",
    "with_stage_ms",
}


def _common_imports(path: Path) -> list[tuple[int, set[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, {a.name for a in node.names})
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "server.code.app.common"
    ]


def test_span_helpers_are_imported_from_their_module_not_the_handler_re_export() -> None:
    offenders = [
        f"{p.relative_to(_ROOT)}:{line} {sorted(names & _SPAN_NAMES)}"
        for p in sorted((_ROOT / "server").rglob("*.py"))
        if p != _ROOT / "server/code/app/common.py"
        for line, names in _common_imports(p)
        if names & _SPAN_NAMES
    ]
    assert offenders == []
