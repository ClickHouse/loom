"""Python import spellings resolve the parser's retained module-qualified names."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.index import symbol_forms as sf
from server.code.domain.extract.lang_python import parse_python


def _definitions(path: str, source: bytes) -> list[dict[str, Any]]:
    return [
        {
            "symbol_id": symbol.id,
            "qualified_name": symbol.qualified_name,
            "name": symbol.name,
            "kind": symbol.kind,
            "path": path,
            "tier": "treesitter",
        }
        for symbol in parse_python(path, source).symbols
    ]


class Repo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, int]] = []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        assert (org, ns) == ("org", "repo")
        self.calls.append((name, limit))
        return [row for row in self.rows if row["name"] == name][:limit]

    async def grep_chunks(self, *args, **kwargs):
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "written"),
    [
        ("src/pkg/json/__init__.py", "src.pkg.json::dumps"),
        ("src/pkg/json/__init__.py", "pkg.json.dumps"),
        ("src/pkg/json/__init__.py", "json::dumps"),
        ("src/pkg/json/encoder.py", "src.pkg.json.encoder.dumps"),
        ("src/pkg/json/encoder.py", "pkg.json.encoder.dumps"),
    ],
)
async def test_import_spelling_resolves_existing_function(path: str, written: str) -> None:
    definitions = _definitions(path, b"def dumps(value):\n    return str(value)\n")
    repo = Repo(definitions)
    rows, by = await sf.resolve_tolerant(repo, "org", "repo", written)
    assert rows == definitions
    assert by == "python_module"
    assert repo.calls == [("dumps", sf.TAIL_READ_LIMIT)]


@pytest.mark.asyncio
async def test_module_spelling_preserves_nested_class_and_constructor() -> None:
    definitions = _definitions(
        "src/pkg/__init__.py", b"class Client:\n    def __init__(self):\n        pass\n"
    )
    repo = Repo(definitions)
    rows, by = await sf.resolve_tolerant(repo, "org", "repo", "pkg.Client.__init__")
    assert [row["qualified_name"] for row in rows] == ["src.pkg.__init__::Client::__init__"]
    assert by == "python_module"


@pytest.mark.asyncio
async def test_duplicate_import_suffixes_remain_ambiguous() -> None:
    rows = _definitions("a/pkg/__init__.py", b"def dumps(value):\n    return value\n")
    rows += _definitions("b/pkg/__init__.py", b"def dumps(value):\n    return value\n")
    assert await sf.resolve_tolerant(Repo(rows), "org", "repo", "pkg.dumps") == ([], "")


@pytest.mark.asyncio
async def test_python_alias_does_not_rewrite_another_language() -> None:
    rows = [
        {
            "symbol_id": "native",
            "qualified_name": "pkg.__init__::dumps",
            "name": "dumps",
            "kind": "function",
            "path": "src/json.cpp",
            "tier": "treesitter",
        }
    ]
    assert await sf.resolve_tolerant(Repo(rows), "org", "repo", "pkg.dumps") == ([], "")


@pytest.mark.asyncio
async def test_incomplete_candidate_pool_never_claims_a_unique_alias() -> None:
    rows = _definitions("pkg/__init__.py", b"def dumps(value):\n    return value\n")
    rows += [{**rows[0], "qualified_name": f"other{i}::dumps"} for i in range(sf.TAIL_READ_LIMIT)]
    assert await sf.resolve_tolerant(Repo(rows), "org", "repo", "pkg.dumps") == ([], "")
