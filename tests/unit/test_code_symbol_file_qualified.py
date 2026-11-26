"""A filename in a symbol request is a lookup constraint."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.symbol.resolve import _resolve_symbols


class _Repo:
    async def symbols_by_name(
        self, org: str, ns: str, name: str, *, path: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        row = {
            "path": "src/blueprints.py",
            "qualified_name": "src.blueprints::BlueprintSetupState::add_url_rule",
            "symbol_id": "definition",
        }
        if name == "BlueprintSetupState.add_url_rule" and path in ("", row["path"]):
            return [row]
        return []


@pytest.mark.asyncio
async def test_file_qualified_name_resolves_the_existing_definition() -> None:
    rows = await _resolve_symbols(
        _Repo(), "o", "n", symbol_id="", name="./src/blueprints.py:BlueprintSetupState.add_url_rule"
    )
    assert [(r["symbol_id"], r["path"], r["resolved_by"]) for r in rows] == [
        ("definition", "src/blueprints.py", "file_symbol")
    ]


@pytest.mark.asyncio
async def test_a_missing_file_never_falls_back_to_another_files_symbol() -> None:
    rows = await _resolve_symbols(
        _Repo(), "o", "n", symbol_id="", name="src/missing.py:BlueprintSetupState.add_url_rule"
    )
    assert rows == []


def test_file_symbol_parser_preserves_namespaces_and_leaves_locations_alone() -> None:
    from server.code.app.symbol.resolve import _parse_file_symbol

    assert _parse_file_symbol("src/Foo.cpp:DB::Foo::bar") == ("src/Foo.cpp", "DB::Foo::bar")
    assert _parse_file_symbol("src/x.py:Type.method") == ("src/x.py", "Type.method")
    for name in (
        "src/x.py:12",
        "module.name::Type::method",
        "DB::Foo::bar",
        "Type.method",
        "x.py:",
    ):
        assert _parse_file_symbol(name) is None
