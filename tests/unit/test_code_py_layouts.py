"""The Python code index reads any repository layout — the
roots its manifests declare, vendored trees, stubs, notebooks, entry points,
pytest trees — and a file the parser fails on stays a text chunk."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.app.index import ingest_parse, ingest_resolve
from server.code.app.index.ingest_chunks import _chunks_for
from server.code.app.index.ingest_parse import parse_file, parse_for_ingest
from server.code.domain.extract import py_layouts as pl
from server.code.domain.extract import py_notebooks as nb
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.lang_python import (
    def_paths_of_scope,
    import_candidates,
    module_scope,
    resolve_imports,
)
from server.code.domain.extract.name_scope import gate_name_table
from server.code.store.index import is_noise_path

PYPROJECT = """
[project]
name = "srcpkg"

[project.scripts]
srcpkg-cli = "srcpkg.cli:main"
srcpkg-mod = "srcpkg.run"

[project.gui-scripts]
srcpkg-gui = "srcpkg.gui:main"

[project.entry-points."srcpkg.plugins"]
echo = "srcpkg.plugins.echo:EchoPlugin"

[tool.setuptools]
package-dir = {"" = "lib", "other" = "ext/other"}

[tool.setuptools.packages.find]
where = ["lib"]

[tool.pytest.ini_options]
pythonpath = ["tests/support", "tests"]
"""

SETUP_CFG = """
[options]
package_dir =
    =src
[options.packages.find]
where = src
[options.entry_points]
console_scripts =
    cfg-tool = cfgpkg.cli:main
gui_scripts =
    cfg-gui = cfgpkg.gui:run
[tool:pytest]
pythonpath = tests
"""

SETUP_PY = """
from setuptools import find_packages, setup

setup(
    name="legacy",
    package_dir={"": "python"},
    packages=find_packages(where="python"),
    entry_points={
        "console_scripts": [
            "legacy-tool = legacy.tool:main",
            "legacy-other=legacy.other:go",
        ],
    },
)
"""


# ---------------------------------------------------------------------------
# roots
# ---------------------------------------------------------------------------


def test_pyproject_roots_follow_the_build_backends():
    roots = pl.roots_from_manifest("pkg/pyproject.toml", PYPROJECT)
    assert roots[0] == "pkg", "the manifest's own directory is a package root"
    assert "pkg/lib" in roots and "pkg/ext" in roots, "package-dir '' and pkg → lib/pkg"
    assert "pkg/tests/support" in roots and "pkg/tests" in roots, "pytest pythonpath"
    poetry = pl.roots_from_manifest(
        "pyproject.toml", '[tool.poetry]\npackages = [{include = "p", from = "src"}]\n'
    )
    assert poetry == ["", "src"]
    hatch = pl.roots_from_manifest(
        "pyproject.toml",
        '[tool.hatch.build.targets.wheel]\npackages = ["python/p"]\nsources = ["python"]\n',
    )
    assert hatch == ["", "python"]
    assert pl.roots_from_manifest("x/pyproject.toml", "not = [toml") == ["x"], (
        "a manifest tomllib rejects still makes its directory a root"
    )


def test_setup_cfg_setup_py_and_pytest_ini_roots():
    assert pl.roots_from_manifest("setup.cfg", SETUP_CFG) == ["", "src", "tests"]
    assert pl.roots_from_manifest("legacy/setup.py", SETUP_PY) == ["legacy", "legacy/python"]
    assert pl.roots_from_manifest("pytest.ini", "[pytest]\npythonpath = src tests\n") == [
        "src",
        "tests",
    ]
    assert pl.roots_from_manifest("tox.ini", "[tox]\nenvlist = py\n") == []
    assert pl.roots_from_manifest("pyproject.toml", '[tool.x]\ny = "../up"\n') == [""], (
        "a root above the repository is dropped"
    )


def test_conftest_outside_a_package_is_a_root():
    present = {
        "tests/conftest.py",
        "tests/helpers.py",
        "pkg/tests/conftest.py",
        "pkg/tests/__init__.py",
        "conftest.py",
    }
    assert pl.conftest_roots(present) == ["", "tests"], "a conftest inside a package is not"


def test_layout_orders_shallow_manifests_first_and_scopes_vendored_roots():
    texts = {
        "tests/data/src/simple/setup.py": "from setuptools import setup\nsetup(name='simple')\n",
        "pyproject.toml": PYPROJECT,
        "src/app/_vendor/lib/pyproject.toml": "[project]\nname = 'lib'\n",
    }
    layout = pl.layout_from_texts(texts, set(texts) | {"tests/conftest.py"})
    assert layout.manifests[0] == "pyproject.toml"
    assert layout.roots[0] == "" and "tests/data/src/simple" in layout.roots
    assert "src/app/_vendor/lib" in layout.roots
    assert "src/app/_vendor/lib" not in layout.roots_for("src/app/main.py"), (
        "a vendored root serves the files under it only"
    )
    assert "src/app/_vendor/lib" in layout.roots_for("src/app/_vendor/lib/lib/x.py")
    assert pl.is_vendored("src/pip/_vendor/requests/api.py") and not pl.is_vendored("src/pip/x.py")
    assert layout.notes == {}
    empty = pl.layout_from_texts({"pyproject.toml": ""}, set())
    assert empty.notes == {"pyproject.toml": "empty"} and empty.roots == ()


def test_import_candidates_take_the_manifest_roots_after_the_ancestors():
    cands = import_candidates("tests/unit/test_x.py", "srcpkg.cli", roots=("lib",))
    assert (
        cands.index("tests/unit/srcpkg/cli.py")
        < cands.index("srcpkg/cli.py")
        < cands.index("lib/srcpkg/cli.py")
    )
    present = {"lib/srcpkg/cli.py", "lib/srcpkg/__init__.py", "tests/unit/test_x.py"}
    assert resolve_imports("tests/unit/test_x.py", ["srcpkg.cli"], present) == []
    assert resolve_imports("tests/unit/test_x.py", ["srcpkg.cli"], present, roots=("lib",)) == [
        "lib/srcpkg/cli.py"
    ]
    # a namespace package: no __init__.py on the way
    present = {"nsroot/company/tools/util.py", "app.py"}
    assert resolve_imports("app.py", ["company.tools.util"], present, roots=("nsroot",)) == [
        "nsroot/company/tools/util.py"
    ]


def test_vendored_copies_resolve_only_from_inside_the_vendor_tree():
    present = {
        "src/app/main.py",
        "src/app/_vendor/tinylib/__init__.py",
        "src/app/_vendor/tinylib/helpers.py",
    }
    inside = resolve_imports("src/app/_vendor/tinylib/helpers.py", ["tinylib"], present)
    assert inside == ["src/app/_vendor/tinylib/__init__.py"]
    assert resolve_imports("src/app/main.py", ["tinylib"], present, roots=("src",)) == []
    assert resolve_imports("src/app/main.py", ["app._vendor.tinylib"], present, roots=("src",)) == [
        "src/app/_vendor/tinylib/__init__.py"
    ]


# ---------------------------------------------------------------------------
# stubs and notebooks
# ---------------------------------------------------------------------------


def test_a_stub_is_its_own_module_and_ranks_after_its_implementation():
    assert module_scope("pkg/foo.pyi") == "pkg.foo.__stub__"
    assert module_scope("pkg/__init__.pyi") == "pkg.__init__.__stub__"
    assert def_paths_of_scope("pkg.foo.__stub__") == ("pkg/foo.pyi", "pkg/foo/__init__.pyi")
    assert def_paths_of_scope("pkg.__init__.__stub__") == ("pkg/__init__.pyi",) * 2
    impl = parse_file("pkg/foo.py", b"def f(x):\n    return x\n")
    stub = parse_file("pkg/foo.pyi", b"def f(x: int) -> int: ...\n")
    assert stub is not None and stub.lang == "python"
    assert {s.qualified_name for s in impl.symbols} == {"pkg.foo::f"}
    assert {s.qualified_name for s in stub.symbols} == {"pkg.foo.__stub__::f"}
    assert is_noise_path("pkg/foo.pyi") and not is_noise_path("pkg/foo.py")
    # the implementation is the import target when both exist; the stub when it is alone
    both = {"pkg/foo.py", "pkg/foo.pyi", "app.py"}
    assert resolve_imports("app.py", ["pkg.foo"], both) == ["pkg/foo.py"]
    assert resolve_imports("app.py", ["pkg.foo"], {"pkg/foo.pyi", "app.py"}) == ["pkg/foo.pyi"]


def test_a_call_binds_the_stub_only_module_through_its_import():
    files = {
        "lib/srcpkg/_speed.pyi": b"def fast(n: int) -> int: ...\n",
        "lib/srcpkg/cli.py": b"from srcpkg._speed import fast\n\ndef main():\n    return fast(3)\n",
    }
    parsed = {p: parse_file(p, s) for p, s in files.items()}
    table: dict = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    cli = parsed["lib/srcpkg/cli.py"]
    cli.includes = resolve_imports(cli.path, cli.includes, set(files), roots=("lib",))
    assert cli.includes == ["lib/srcpkg/_speed.pyi"]
    ctx = {"py_present": set(files), "py_roots": ("lib",)}
    edges, _ = extract_edges_and_misses(cli, gate_name_table(table, "python"), None, None, ctx)
    calls = [(e.to_qualified_name, e.confidence) for e in edges if e.edge_kind == "calls"]
    assert calls == [("lib.srcpkg._speed.__stub__::fast", 1.0)]


def test_a_notebook_parses_as_its_code_cells_with_a_line_map():
    raw = json.dumps(
        {
            "cells": [
                {"cell_type": "markdown", "source": ["# title"]},
                {"cell_type": "code", "source": ["%matplotlib inline\n", "import os\n", "!ls\n"]},
                {"cell_type": "code", "source": "def f(x):\n    return x\n\nf(1)"},
            ]
        }
    ).encode()
    text = nb.notebook_source(raw)
    assert text is not None
    lines = text.decode().splitlines()
    assert (
        lines[0] == "# %% [cell 1]" and lines[1] == "# %matplotlib inline" and lines[3] == "# !ls"
    )
    assert lines[4] == "# %% [cell 2]" and lines[5] == "def f(x):"
    assert [(f.name, f.line) for f in nb.cell_facets(text)] == [("cell 1", 1), ("cell 2", 5)]
    assert nb.cell_of_line(text, 6) == (2, 1) and nb.cell_of_line(text, 1) is None
    pf = parse_file("nb/analysis.ipynb", raw)
    assert pf is not None and pf.lang == "python" and pf.tree is not None
    assert [(s.qualified_name, s.start_line) for s in pf.symbols] == [
        ("nb.analysis.__notebook__::f", 6)
    ]
    assert pf.includes == ["os"] and pf.source == text
    assert def_paths_of_scope("nb.analysis.__notebook__") == ("nb/analysis.ipynb",) * 2
    v3 = json.dumps(
        {"worksheets": [{"cells": [{"cell_type": "code", "input": ["x = 1\n"]}]}]}
    ).encode()
    assert nb.notebook_source(v3) == b"# %% [cell 0]\nx = 1\n"
    assert nb.notebook_source(b"not a notebook") is None
    assert (
        nb.notebook_source(
            json.dumps({"cells": [{"cell_type": "markdown", "source": "x"}]}).encode()
        )
        is None
    )
    broken = parse_file("bad.ipynb", b"{}")
    assert broken is not None and broken.lang == "python" and broken.tree is None, (
        "a malformed notebook stays a chunk-only file"
    )


def test_python_shaped_files_are_detected_and_cython_is_text():
    assert parse_file("a/b.pyw", b"def w():\n    pass\n").symbols[0].qualified_name == "a.b::w"
    assert parse_file("ext/fast.pyx", b"cdef int f(int x):\n    return x\n" * 20).lang == "text"
    assert parse_file("ext/fast.pxd", b"cdef class C:\n    pass\n" * 20).lang == "text"


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def test_entry_points_from_every_manifest_form():
    eps = pl.entry_points_from_manifest("pyproject.toml", PYPROJECT)
    assert [(e.name, e.group, e.module, e.attr, e.line) for e in eps] == [
        ("srcpkg-cli", "console_scripts", "srcpkg.cli", "main", 6),
        ("srcpkg-mod", "console_scripts", "srcpkg.run", "", 7),
        ("srcpkg-gui", "gui_scripts", "srcpkg.gui", "main", 10),
        ("echo", "srcpkg.plugins", "srcpkg.plugins.echo", "EchoPlugin", 13),
    ]
    cfg = pl.entry_points_from_manifest("setup.cfg", SETUP_CFG)
    assert [(e.name, e.group, e.module, e.attr, e.line) for e in cfg] == [
        ("cfg-tool", "console_scripts", "cfgpkg.cli", "main", 9),
        ("cfg-gui", "gui_scripts", "cfgpkg.gui", "run", 11),
    ]
    py = pl.entry_points_from_manifest("legacy/setup.py", SETUP_PY)
    assert [(e.name, e.module, e.attr, e.line) for e in py] == [
        ("legacy-tool", "legacy.tool", "main", 10),
        ("legacy-other", "legacy.other", "go", 11),
    ]
    ini_form = 'setup(entry_points="""\n[console_scripts]\nt = t.cli:main\n""")\n'
    assert [
        (e.name, e.module, e.attr) for e in pl.entry_points_from_manifest("setup.py", ini_form)
    ] == [("t", "t.cli", "main")]
    assert pl.entry_points_from_manifest("pytest.ini", "[pytest]\n") == []


def test_entry_point_facets_land_on_the_manifest_and_the_target_function():
    mf = pl.manifest_facets("pyproject.toml", PYPROJECT)
    assert [(f.kind, f.name, f.line) for f in mf][:2] == [
        ("entry_point", "srcpkg-cli", 6),
        ("entry_point", "srcpkg-mod", 7),
    ]
    layout = pl.layout_from_texts({"pyproject.toml": PYPROJECT}, {"pyproject.toml"})
    cli = parse_file("lib/srcpkg/cli.py", b"import sys\n\n\ndef main():\n    return 0\n")
    assert [(f.name, f.line, f.confidence) for f in pl.module_entry_facets(cli, layout)] == [
        ("srcpkg-cli", 4, 1.0)
    ]
    run = parse_file("lib/srcpkg/run.py", b"print('hi')\n")
    assert [(f.name, f.line, f.confidence) for f in pl.module_entry_facets(run, layout)] == [
        ("srcpkg-mod", 1, 1.0)
    ]
    gui = parse_file("lib/srcpkg/gui.py", b"main = object()\n")
    assert [(f.name, f.line, f.confidence) for f in pl.module_entry_facets(gui, layout)] == [
        ("srcpkg-gui", 1, 1.0)
    ], "an attribute: the module-level assignment is a definition, the facet lands on it at 1.0"
    assert pl.module_entry_facets(parse_file("other/cli.py", b"def main(): pass\n"), layout) == []
    main_py = parse_file(
        "lib/srcpkg/__main__.py",
        b"from .cli import main\n\nif __name__ == '__main__':\n    main()\n",
    )
    assert [(f.name, f.line) for f in pl.script_facets(main_py)] == [
        ("python -m srcpkg", 1),
        ("python lib/srcpkg/__main__.py", 3),
    ]
    assert pl.script_facets(parse_file("lib/x.py", b"def f():\n    pass\n")) == []


def test_the_ingest_hands_the_facets_to_the_symbol_stage():
    key = ("org", "ns-11760")
    layout = pl.layout_from_texts({"pyproject.toml": PYPROJECT}, {"pyproject.toml"})
    ingest_resolve._PY_LAYOUT_CACHE[key] = (time.monotonic(), layout)
    try:
        pf = parse_file("pyproject.toml", PYPROJECT.encode())
        assert pf is not None and pf.lang == "config"
        _c, _p, facets = _chunks_for(pf, pf.path, pf.source, 6000, key)
        assert [f.name for f in facets] == ["srcpkg-cli", "srcpkg-mod", "srcpkg-gui", "echo"]
        assert ingest_parse._facets_ride_symbols(pf, pf.path, False, facets)
        cli = parse_file("lib/srcpkg/cli.py", b"def main():\n    return 0\n")
        _c, _p, facets = _chunks_for(cli, cli.path, cli.source, 6000, key)
        assert [(f.kind, f.name, f.line) for f in facets] == [("entry_point", "srcpkg-cli", 1)]
        legacy = parse_file("legacy/setup.py", SETUP_PY.encode())
        _c, _p, facets = _chunks_for(legacy, legacy.path, legacy.source, 6000, key)
        assert [f.name for f in facets] == ["legacy-tool", "legacy-other"]
        cold = parse_file("lib/srcpkg/cli.py", b"def main():\n    return 0\n")
        assert _chunks_for(cold, cold.path, cold.source, 6000, ("org", "other"))[2] == [], (
            "no layout read yet: no module-side facet (the heal re-derives)"
        )
    finally:
        ingest_resolve._PY_LAYOUT_CACHE.pop(key, None)


# ---------------------------------------------------------------------------
# parse failures stay indexed
# ---------------------------------------------------------------------------


def test_a_parser_fault_keeps_the_file_as_text_chunks_with_the_error_recorded(monkeypatch):
    def boom(path: str, source: bytes):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setitem(ingest_parse._PARSERS, "python", boom)
    src = b"# -*- coding: latin-1 -*-\n" + b"x = '\xe9'\n" * 40
    pf, err = parse_for_ingest("legacy/old.py", src)
    assert pf is not None and pf.lang == "python" and pf.tree is None and pf.symbols == []
    assert err == "parse: RecursionError"
    chunks, _p, facets = _chunks_for(pf, pf.path, src, 6000, None)
    assert chunks and all(c.path == "legacy/old.py" for c in chunks), "the text-fallback chunks"
    assert facets == []


def test_python_two_and_a_bom_still_parse_to_symbols():
    bom = parse_file("bom.py", b"\xef\xbb\xbfimport os\n\n\ndef f():\n    return 1\n")
    assert [s.name for s in bom.symbols] == ["f"] and bom.includes == ["os"]
    py2 = parse_file("py2.py", b"print 'x'\n\n\ndef g(y):\n    print y\n    return y\n")
    assert [s.name for s in py2.symbols] == ["g"]


def test_the_layout_is_read_off_the_event_loop(monkeypatch):
    # A manifest is repository text up to its cap: reading it must not hold the loop.
    threads = []

    async def text(_repo, _org, _ns, _path):
        return "setup()"

    def layout(texts, present):
        threads.append(threading.current_thread())
        return pl.Layout()

    monkeypatch.setitem(ingest_resolve._LIVE_PATHS_CACHE, ("o", "n"), (time.monotonic(), set()))
    monkeypatch.setattr(ingest_resolve, "_PY_LAYOUT_CACHE", {})
    monkeypatch.setattr(ingest_resolve, "_package_json_text", text)
    monkeypatch.setattr(pl, "layout_from_texts", layout)
    asyncio.run(ingest_resolve._py_layout(None, "o", "n", {"setup.py"}))  # type: ignore[arg-type]
    assert threads and threads[0] is not threading.main_thread()


def test_files_that_miss_the_layout_cache_at_once_share_one_read(monkeypatch):
    calls = []

    async def text(_repo, _org, _ns, _path):
        return "setup()"

    def layout(texts, present):
        calls.append(1)
        return pl.Layout()

    monkeypatch.setitem(ingest_resolve._LIVE_PATHS_CACHE, ("o", "n"), (time.monotonic(), set()))
    monkeypatch.setattr(ingest_resolve, "_PY_LAYOUT_CACHE", {})
    monkeypatch.setattr(ingest_resolve, "_package_json_text", text)
    monkeypatch.setattr(pl, "layout_from_texts", layout)

    async def three():
        misses = [ingest_resolve._py_layout(None, "o", "n", {"setup.py"}) for _ in range(3)]  # type: ignore[arg-type]
        return await asyncio.gather(*misses)

    assert len(asyncio.run(three())) == 3
    assert calls == [1]


def test_a_list_inside_an_unclosed_entry_point_list_is_read_once():
    text = 'setup(entry_points={"console_scripts": ["a=m:f",\n"gui_scripts": ["g=m:h"]})\n'
    eps = pl.entry_points_from_manifest("setup.py", text)
    assert [(e.name, e.group, e.line) for e in eps] == [
        ("a", "console_scripts", 1),
        ("g", "console_scripts", 2),
    ]


def test_a_layout_keeps_the_first_entry_points_up_to_the_cap():
    text = "[options.entry_points]\nconsole_scripts =\n" + "".join(
        f"  a{i} = m:f\n" for i in range(pl.MAX_ENTRY_POINTS + 5)
    )
    eps = pl.layout_from_texts({"setup.cfg": text}, []).entry_points
    assert len(eps) == pl.MAX_ENTRY_POINTS and eps[-1].name == f"a{pl.MAX_ENTRY_POINTS - 1}"
