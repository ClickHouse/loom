"""Keep the handwritten Rust search hit aligned with the server's wire fields."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_rust_search_hit_covers_server_wire_fields() -> None:
    tree = ast.parse((ROOT / "server/memory/api/models_search.py").read_text())
    model = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_MemorySearchHit"
    )
    expected = {
        node.target.id
        for node in model.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and not node.target.id.startswith("_")
    }
    source = (ROOT / "sdk/rust/src/models.rs").read_text()
    hit = source.split("pub struct SearchHit {", 1)[1].split("\n}", 1)[0]
    actual = set(re.findall(r"pub (\w+):", hit))
    assert actual == expected, f"missing={expected - actual}, extra={actual - expected}"
