"""A Python call binds through its imports; a library name
never binds a repository namesake; a Python call never binds another
script language's definition; imports are file edges."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.lang_python import (
    def_paths_of_scope,
    external_names,
    import_candidates,
    py_import_bindings,
    py_star_imports,
    resolve_imports,
)
from server.code.domain.extract.name_scope import gate_name_table

FILES = {
    "pkg/__init__.py": b"from .impl import helper\n",
    "pkg/impl.py": b"def helper():\n    return 1\n\n\nclass Store:\n    def save(self):\n        return 2\n\n    @classmethod\n    def make(cls):\n        return cls()\n",
    "pkg/util/__init__.py": b"",
    "pkg/util/text.py": b"def dumps(x):\n    return str(x)\n\n\ndef decode(b):\n    return b\n",
    "pkg/util/extra.py": b"def loads(x):\n    return x\n",
    "app/main.py": (
        b"import json\n"
        b"import pkg.util.text as txt\n"
        b"import pkg.util\n"
        b"from pkg import helper\n"
        b"from pkg.impl import Store\n"
        b"from .local import sibling\n"
        b"from pkg.util.extra import *\n"
        b"\n"
        b"def run(raw, store):\n"
        b"    json.dumps(raw)\n"
        b"    txt.dumps(raw)\n"
        b"    pkg.util.text.dumps(raw)\n"
        b"    helper()\n"
        b"    Store.make()\n"
        b"    sibling()\n"
        b"    loads(raw)\n"
        b"    raw.decode()\n"
        b"    store.save()\n"
        b"    s = Store()\n"
        b"    s.save()\n"
        b"    return raw.encode().decode()\n"
        b"\n"
        b"def lazy():\n"
        b"    from pkg.util.text import decode\n"
        b"    return decode(b'')\n"
    ),
    "app/local.py": b"def sibling():\n    return 3\n",
    "web/client.py": b"class Fake:\n    def dumps(self):\n        return 0\n",
}


def _corpus():
    present = set(FILES)
    parsed = {}
    name_table: dict[str, list[tuple[str, str, str]]] = {}
    for rel, src in FILES.items():
        pf = parse_file(rel, src)
        assert pf is not None
        parsed[rel] = pf
        for s in pf.symbols:
            name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return present, parsed, name_table


def _edges(rel: str, *, ts_namesake: bool = False):
    present, parsed, name_table = _corpus()
    if ts_namesake:
        # a TypeScript definition under the shared module-scoped scheme
        name_table.setdefault("decode", []).append(("ts1", "web.src.codec::decode", "function"))
        name_table.setdefault("sibling", []).append(("ts2", "web.src.codec::sibling", "function"))
    pf = parsed[rel]
    pf.includes = [p for p in resolve_imports(rel, list(pf.includes), present) if p != rel]
    gated = gate_name_table(name_table, "python")
    edges, misses = extract_edges_and_misses(pf, gated, None, None, {"py_present": present})
    return pf, edges, misses


def _calls(edges):
    return {
        (e.line, e.to_qualified_name, round(e.confidence, 2))
        for e in edges
        if e.edge_kind == "calls"
    }


def test_import_specs_and_bindings_cover_every_form():
    pf = parse_file("app/main.py", FILES["app/main.py"])
    assert "json" in pf.includes and "pkg.util.text" in pf.includes and ".local" in pf.includes
    assert "pkg.impl.Store" in pf.includes, "a from-import's member may be a submodule"
    b = py_import_bindings(pf)
    assert b["json"] == ("json", "")
    assert b["txt"] == ("pkg.util.text", "")
    assert b["pkg.util"] == ("pkg.util", "") and b["pkg"] == ("pkg", "")
    assert b["helper"] == ("pkg", "helper")
    assert b["Store"] == ("pkg.impl", "Store")
    assert b["sibling"] == (".local", "sibling")
    assert b["decode"] == ("pkg.util.text", "decode"), "an import inside a function body binds too"
    assert py_star_imports(pf) == ["pkg.util.extra"]


def test_candidates_follow_the_interpreter():
    # the stub forms (`.pyi`) come after the interpreter's two
    assert import_candidates("app/main.py", ".local")[:2] == [
        "app/local.py",
        "app/local/__init__.py",
    ]
    assert import_candidates("pkg/sub/m.py", "..impl")[:1] == ["pkg/impl.py"]
    cands = import_candidates("tests/unit/test_x.py", "server.core.ch")
    assert "server/core/ch.py" in cands and "src/server/core/ch.py" in cands
    assert cands.index("tests/unit/server/core/ch.py") < cands.index("server/core/ch.py"), (
        "nearest root first"
    )
    assert def_paths_of_scope("pkg.util.text") == ("pkg/util/text.py", "pkg/util/text/__init__.py")
    assert def_paths_of_scope("pkg.__init__") == ("pkg/__init__.py", "pkg/__init__.py")


def test_imports_are_file_edges_and_libraries_are_not():
    pf, edges, _ = _edges("app/main.py")
    assert set(pf.includes) == {
        "pkg/util/text.py",
        "pkg/util/__init__.py",
        "pkg/__init__.py",
        "pkg/impl.py",
        "app/local.py",
        "pkg/util/extra.py",
    }
    assert external_names(parse_file("app/main.py", FILES["app/main.py"]), set(pf.includes)) == {
        "json"
    }


def test_calls_bind_through_imports():
    _, edges, misses = _edges("app/main.py")
    calls = _calls(edges)
    assert (11, "pkg.util.text::dumps", 1.0) in calls, "`txt.dumps()` through the alias"
    assert (12, "pkg.util.text::dumps", 1.0) in calls, (
        "`pkg.util.text.dumps()` through the dotted import"
    )
    assert (13, "pkg.impl::helper", 0.9) in calls, (
        "`from pkg import helper` re-exported by the package"
    )
    assert (14, "pkg.impl::Store::make", 1.0) in calls, "`Store.make()` on the imported class"
    assert (15, "app.local::sibling", 1.0) in calls, "a relative import"
    assert (16, "pkg.util.extra::loads", 0.9) in calls, "a star import's one definition"
    assert (25, "pkg.util.text::decode", 1.0) in calls, "the function-body import"


def test_a_library_call_binds_no_namesake():
    _, edges, misses = _edges("app/main.py")
    calls = _calls(edges)
    assert not any(q.endswith("::dumps") and line == 10 for line, q, _ in calls), "`json.dumps()`"
    assert not any(line == 17 for line, _q, _ in calls), "`raw.decode()` on an untyped receiver"
    assert not any(line == 21 for line, _q, _ in calls), "`raw.encode().decode()` on an expression"
    # the Fake's dumps in web/client.py never caught json.dumps either
    assert not any(q.startswith("web.client") for _l, q, _ in calls)


def test_a_typed_receiver_still_binds_its_method():
    _, edges, _ = _edges("app/main.py")
    calls = _calls(edges)
    assert any(line == 20 and q == "pkg.impl::Store::save" for line, q, _ in calls), (
        "`s = Store(); s.save()`"
    )


def test_a_python_call_never_binds_another_languages_definition():
    _, edges, _ = _edges("app/main.py", ts_namesake=True)
    calls = _calls(edges)
    assert (15, "app.local::sibling", 1.0) in calls
    assert not any(q.startswith("web.src.codec") for _l, q, _ in calls)


TYPED_FILES = {
    "svc/ch.py": b"class CHClient:\n    async def query_all(self, sql):\n        return []\n\n\ndef get_client() -> CHClient:\n    return CHClient()\n",
    "svc/base.py": b"from svc.ch import get_client\n\n\nclass _CHClient:\n    async def query_all(self, sql):\n        return []\n\n\ndef _default_client() -> _CHClient:\n    return get_client()\n",
    "svc/other.py": b"class _CHClient:\n    async def query_all(self, sql):\n        return []\n",
    "svc/repo.py": (
        b"from svc.base import _CHClient, _default_client\n"
        b"from svc.ch import get_client\n"
        b"\n"
        b"class Repo:\n"
        b"    def __init__(self, ch: _CHClient | None = None):\n"
        b"        self._ch = ch or _default_client()\n"
        b"\n"
        b"    async def rows(self):\n"
        b"        return await self._ch.query_all('x')\n"
        b"\n"
        b"\n"
        b"async def direct(ch: _CHClient | None = None):\n"
        b"    ch = ch or _default_client()\n"
        b"    await ch.query_all('a')\n"
        b"    return await get_client().query_all('b')\n"
    ),
}


def _typed_edges(rel: str):
    present = set(TYPED_FILES)
    parsed = {}
    name_table: dict[str, list[tuple[str, str, str]]] = {}
    returns: dict[str, str] = {}
    for p, src in TYPED_FILES.items():
        pf = parse_file(p, src)
        parsed[p] = pf
        for s in pf.symbols:
            name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
            if s.return_type:
                returns[s.qualified_name] = s.return_type
    from server.code.domain.extract.edges_treesitter import callee_targets

    pf = parsed[rel]
    pf.includes = [p for p in resolve_imports(rel, list(pf.includes), present) if p != rel]
    gated = gate_name_table(name_table, "python")
    ctx = {"py_present": present}
    targets = callee_targets(pf, gated, ctx)
    callee_returns = {c: returns[q] for c, q in targets.items() if returns.get(q)}
    edges, _ = extract_edges_and_misses(pf, gated, None, callee_returns or None, ctx)
    return _calls(edges)


def test_a_same_named_class_is_told_apart_by_the_import():
    calls = _typed_edges("svc/repo.py")
    # `ch: _CHClient | None` — two classes named _CHClient exist; the import names svc.base's
    assert any(line == 14 and q == "svc.base::_CHClient::query_all" for line, q, _ in calls)
    assert not any(q.startswith("svc.other") for _l, q, _ in calls)


def test_an_or_binding_keeps_the_typed_operand():
    calls = _typed_edges("svc/repo.py")
    # `self._ch = ch or _default_client()` types the field from the parameter / the callee
    assert any(line == 9 and q == "svc.base::_CHClient::query_all" for line, q, _ in calls)


def test_a_call_receiver_is_typed_by_its_callees_return():
    calls = _typed_edges("svc/repo.py")
    # `get_client().query_all()` — the callee returns CHClient (read cross-file)
    assert any(line == 15 and q == "svc.ch::CHClient::query_all" for line, q, _ in calls)


CHAIN_FILES = {
    "web/__init__.py": b"from .params import Depends as Depends\nfrom .routing import *\n",
    "web/params.py": b"def Depends(f):\n    return f\n",
    "web/models.py": b"class Depends:\n    pass\n",
    "web/routing.py": b"def route(path):\n    return path\n",
    "app/handlers.py": b"from web import Depends, route\n\n\ndef h():\n    Depends(h)\n    route('/x')\n",
}


def _chain_edges(rel: str, *, with_texts: bool):
    present = set(CHAIN_FILES)
    parsed = {}
    name_table: dict[str, list[tuple[str, str, str]]] = {}
    for p, src in CHAIN_FILES.items():
        pf = parse_file(p, src)
        parsed[p] = pf
        for s in pf.symbols:
            name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    pf = parsed[rel]
    pf.includes = [p for p in resolve_imports(rel, list(pf.includes), present) if p != rel]
    ctx: dict = {"py_present": present}
    if with_texts:
        ctx["py_init_texts"] = {
            p: src.decode() for p, src in CHAIN_FILES.items() if p.endswith("/__init__.py")
        }
    edges, misses = extract_edges_and_misses(
        pf, gate_name_table(name_table, "python"), None, None, ctx
    )
    return _calls(edges), misses


def test_a_package_reexport_is_followed_to_its_declaration():
    # two definitions named Depends under the package: only the __init__ says which one
    calls, misses = _chain_edges("app/handlers.py", with_texts=True)
    assert (5, "web.params::Depends", 1.0) in calls
    assert (6, "web.routing::route", 1.0) in calls, "a star re-export narrows to the starred module"
    calls_without, _ = _chain_edges("app/handlers.py", with_texts=False)
    assert not any(q.endswith("::Depends") for _l, q, _ in calls_without), (
        "ambiguous without the text"
    )
    assert (6, "web.routing::route", 0.9) in calls_without, "the package-unique rung still holds"
