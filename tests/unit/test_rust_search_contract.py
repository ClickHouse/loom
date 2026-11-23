"""Keep the generated Rust search hit aligned with the server's wire fields."""

import ast
import re
from pathlib import Path

import scripts.codegen.rust_schema_fixtures as fixtures
import scripts.codegen.sdk_rust_gen as gen

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
    source = (ROOT / "sdk/rust/src/wire_models.rs").read_text()
    hit = source.split("pub struct MemorySearchHit {", 1)[1].split("\n}", 1)[0]
    actual = set(re.findall(r"pub (\w+):", hit))
    assert actual == expected, f"missing={expected - actual}, extra={actual - expected}"


def test_selected_contracts_and_default_fixtures_are_generated_without_a_toolchain(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gen, "rustfmt", lambda text: text)
    monkeypatch.setattr(fixtures, "rustfmt", lambda text: text)
    source = gen.build(gen.CONVENIENCE_OPS)
    assert all(f'client.call("{op}"' in source for op in gen.CONVENIENCE_OPS)
    fixture = fixtures.build_fixture()
    assert "pub flag: Option<bool>" in fixture
    assert 'serde_json::from_str("false")' in fixture
    assert "DefaultsLabel::Empty" in fixture
