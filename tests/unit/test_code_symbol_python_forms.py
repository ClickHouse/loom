"""Python import spellings resolve the parser's retained module-qualified names."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import symbol_forms as sf
from server.code.app.symbol.resolve import _resolve_symbols
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

    async def symbols_by_name(self, org, ns, name, *, limit=50, path=""):
        assert (org, ns) == ("org", "repo")
        self.calls.append((name, limit))
        return [
            row
            for row in self.rows
            if (not path or row["path"] == path) and name in {row["name"], row["qualified_name"]}
        ][:limit]

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "verified",
        "stale_member",
        "nested_function",
        "duplicate_root",
        "incomplete_source",
        "shadow",
    ],
)
async def test_public_class_member_reexport_requires_current_unambiguous_source(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from server.code.app.index import gitrepo, python_reexports

    init, target = "pkg/__init__.py", "pkg/api.py"
    rev = "a" * 40
    source = b"from .api import Client as Client\n__version__ = '1'\n"
    target_text = b"class Client:\n    def run(self):\n        pass\n"
    if case == "nested_function":
        target_text = b"def Client():\n    def run():\n        pass\n"
    rows = _definitions(target, target_text)
    for row in rows:
        row["indexed_commit"] = rev.encode().ljust(64, b"\0")
        if case == "stale_member" and row["name"] == "run":
            row["indexed_commit"] = "b" * 40
    metadata = {
        path: {
            "indexed_commit": rev,
            "blob_sha": gitrepo.blob_sha(data),
            "lang": "python",
            "chunk_count": 1,
        }
        for path, data in ((init, source), (target, target_text))
    }

    class ReexportRepo(Repo):
        async def file_rows_for_paths(self, org, ns, paths):
            assert (org, ns) == ("org", "repo")
            return {path: metadata[path] for path in paths if path in metadata}

        async def paths_by_suffix(self, org, ns, suffixes, *, limit):
            assert (org, ns, suffixes, limit) == ("org", "repo", [init], 2)
            return {init: ["vendor/" + init]} if case == "duplicate_root" else {}

    def config(org, ns):
        assert (org, ns) == ("org", "repo")
        return SimpleNamespace(path_prefixes=())

    async def present(_):
        return "mirror"

    async def blob(mirror, commit, path, *, max_bytes):
        assert (mirror, commit, path, max_bytes) == ("mirror", rev, init, 16_384)
        return source.splitlines(keepends=True)[0] if case == "incomplete_source" else source

    async def paths(mirror, commit, wanted):
        assert (mirror, commit) == ("mirror", rev)
        competing = "pkg/api/__init__.py"
        assert {target, competing} <= wanted
        return {target, competing} if case == "shadow" else {target}

    monkeypatch.setattr(python_reexports, "repo_for_namespace", config)
    monkeypatch.setattr(python_reexports, "present_dir", present)
    monkeypatch.setattr(gitrepo, "show_file", blob)
    monkeypatch.setattr(gitrepo, "blob_paths_at", paths)
    result = await _resolve_symbols(
        ReexportRepo(rows), "org", "repo", symbol_id="", name="pkg.Client.run"
    )
    expected = [{**row, "resolved_by": "python_module"} for row in rows if row["name"] == "run"]
    assert result == (expected if case == "verified" else [])
