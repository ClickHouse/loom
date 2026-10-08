"""Python imports written around a dependency: every branch of a module
guard (if / elif / else, try / except / else / finally) imports and defines
at module level, and a call through `from m import f as g` binds `m`'s `f`,
never a namesake called `g` and never a library's `f`."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.lang_python import resolve_imports
from server.code.domain.extract.name_scope import gate_name_table

GUARDED = (
    b"import os\n"
    b"if os.name == 'nt':\n"
    b"    from .win import backend\n"
    b"elif os.name == 'java':\n"
    b"    from .jvm import backend\n"
    b"else:\n"
    b"    from .posix import backend\n"
    b"try:\n"
    b"    import fastjson as json\n"
    b"except ImportError:\n"
    b"    from . import slowjson as json\n"
    b"\n"
    b"    def fallback():\n"
    b"        return 0\n"
    b"else:\n"
    b"    from .speedups import accel\n"
    b"finally:\n"
    b"    from .cleanup import done\n"
)


def test_every_branch_of_a_module_guard_is_an_include() -> None:
    pf = parse_file("app/compat.py", GUARDED)
    assert pf is not None
    for spec in (".win", ".jvm", ".posix", "fastjson", ".", ".slowjson", ".speedups", ".cleanup"):
        assert spec in pf.includes, (spec, pf.includes)


def test_a_definition_in_an_except_clause_is_a_module_symbol() -> None:
    pf = parse_file("app/compat.py", GUARDED)
    assert pf is not None
    assert "app.compat::fallback" in {s.qualified_name for s in pf.symbols}


FILES = {
    "pkg/__init__.py": b"",
    "pkg/impl.py": b"def helper():\n    return 1\n",
    "other/fast.py": b"def fast():\n    return 2\n",
    "app/main.py": (
        b"from pkg.impl import helper as fast\n"
        b"from requests import get as fetch\n"
        b"\n"
        b"\n"
        b"def run():\n"
        b"    fast()\n"
        b"    fetch()\n"
    ),
    "app/fetch.py": b"def get():\n    return 3\n",
}


def _calls() -> dict[str, str | None]:
    parsed = {rel: parse_file(rel, src) for rel, src in FILES.items()}
    names: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        assert pf is not None
        for s in pf.symbols:
            names.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    pf = parsed["app/main.py"]
    assert pf is not None
    pf.includes = list(resolve_imports("app/main.py", list(pf.includes), set(FILES)))
    edges, misses = extract_edges_and_misses(
        pf, gate_name_table(names, "python"), None, None, {"py_present": set(FILES)}
    )
    by_line = {e.line: e.to_qualified_name for e in edges if e.edge_kind == "calls"}
    assert not any(getattr(m, "line", 0) == 6 for m in misses)
    return {"fast": by_line.get(6), "fetch": by_line.get(7)}


def test_an_aliased_member_binds_the_imported_definition_not_the_aliass_namesake() -> None:
    assert _calls()["fast"] == "pkg.impl::helper"


def test_an_aliased_library_member_binds_no_repository_namesake() -> None:
    # `get` is defined in the repository, but this `get` is the library's
    assert _calls()["fetch"] is None
