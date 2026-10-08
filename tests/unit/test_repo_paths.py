"""Files the server ships are found from the checkout root, not from a module's depth.

server.core.paths is the one place that knows where the checkout's files are;
a module that counted parent directories from its own __file__ would resolve
the wrong path as soon as it moved to another package.
"""

from __future__ import annotations

import ast
from pathlib import Path

from server.core import paths

ROOT = Path(__file__).resolve().parents[2]


def test_the_checkout_paths_resolve_to_real_files() -> None:
    assert paths.REPO_ROOT == ROOT
    assert any(paths.MIGRATIONS_DIR.glob("*.sql"))
    assert paths.SCHEMA_SQL.is_file()
    assert (paths.SCIP_HARNESS_DIR / "run_delta.sh").is_file()


def test_no_server_module_counts_parents_of_its_own_file() -> None:
    allowed = {"server/core/paths.py", "server/code/domain/extract/extractor_stamp.py"}
    offenders = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"parent", "parents"}
                and "__file__" in ast.unparse(node.value)
                and "Path(__file__)" in ast.unparse(node.value)
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "use server.core.paths instead:\n" + "\n".join(offenders)
