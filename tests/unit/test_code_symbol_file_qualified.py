"""A filename in a symbol request is a lookup constraint."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.symbol.resolve import _resolve_symbols


class _Repo:
    def __init__(self, path: str = "src/blueprints.py", other_files: tuple[str, ...] = ()) -> None:
        self.path = path
        self.files = {path, *other_files}
        self.reads: list[tuple[str, str]] = []

    async def symbols_by_name(
        self, org: str, ns: str, name: str, *, path: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        assert (org, ns) == ("o", "n")
        self.reads.append(("symbol", path))
        row = {
            "path": self.path,
            "qualified_name": "src.blueprints::BlueprintSetupState::add_url_rule",
            "symbol_id": "definition",
        }
        if name == "BlueprintSetupState.add_url_rule" and path in ("", row["path"]):
            return [row]
        return []

    async def file_rows_for_paths(self, org: str, ns: str, paths: list[str]) -> dict[str, Any]:
        assert (org, ns) == ("o", "n")
        (path,) = paths
        self.reads.append(("file", path))
        return {path: {}} if path in self.files else {}

    async def paths_by_suffix(
        self, org: str, ns: str, suffixes: list[str], *, limit: int
    ) -> dict[str, list[str]]:
        assert (org, ns, limit) == ("o", "n", 2)
        (suffix,) = suffixes
        self.reads.append(("suffix", suffix))
        return {suffix: sorted(path for path in self.files if path.endswith("/" + suffix))[:limit]}


@pytest.mark.asyncio
async def test_file_qualified_name_resolves_the_existing_definition() -> None:
    repo = _Repo()
    rows = await _resolve_symbols(
        repo, "o", "n", symbol_id="", name="./src/blueprints.py:BlueprintSetupState.add_url_rule"
    )
    assert [(r["symbol_id"], r["path"], r["resolved_by"]) for r in rows] == [
        ("definition", "src/blueprints.py", "file_symbol")
    ]
    assert repo.reads == [("symbol", "src/blueprints.py")]


@pytest.mark.asyncio
async def test_a_missing_file_never_falls_back_to_another_files_symbol() -> None:
    rows = await _resolve_symbols(
        _Repo(), "o", "n", symbol_id="", name="src/missing.py:BlueprintSetupState.add_url_rule"
    )
    assert rows == []


@pytest.mark.asyncio
async def test_file_qualified_name_resolves_unique_component_suffix() -> None:
    canonical = "src/flask/blueprints.py"
    suffix = "flask/blueprints.py"
    repo = _Repo(canonical, ("src/oldflask/blueprints.py",))
    rows = await _resolve_symbols(
        repo, "o", "n", symbol_id="", name=f"{suffix}:BlueprintSetupState.add_url_rule"
    )
    assert [(row["symbol_id"], row["path"], row["resolved_by"]) for row in rows] == [
        ("definition", canonical, "file_symbol")
    ]
    assert repo.reads == [
        ("symbol", suffix),
        ("file", suffix),
        ("suffix", suffix),
        ("symbol", canonical),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "other_files", "additional_reads"),
    [
        ("flask/blueprints.py", ("flask/blueprints.py",), ["file"]),
        ("flask/blueprints.py", ("vendor/flask/blueprints.py",), ["file", "suffix"]),
        ("/flask/blueprints.py", (), []),
        ("./flask/blueprints.py", (), []),
        ("../flask/blueprints.py", (), []),
        ("blueprints.py", (), []),
    ],
)
async def test_file_suffix_keeps_exact_and_ambiguous_paths_unresolved(
    path: str, other_files: tuple[str, ...], additional_reads: list[str]
) -> None:
    repo = _Repo("src/flask/blueprints.py", other_files)
    rows = await _resolve_symbols(
        repo, "o", "n", symbol_id="", name=f"{path}:BlueprintSetupState.add_url_rule"
    )
    assert rows == []
    assert [kind for kind, _ in repo.reads] == ["symbol", *additional_reads]


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
