"""The Python index carries every kind of definition a
module has — module bindings, class attributes, enum members, stub
declarations — and the facets a def's decorators state (routes, commands,
decorator names, `async`); a decorator's calls belong to the decorated def."""

from __future__ import annotations

from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.lang_python import parse_python, resolve_imports
from server.code.domain.extract.name_scope import gate_name_table
from server.code.domain.extract.py_routes import _PREFIX_DECL_RE, py_facets

MODULE = b'''
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias, TypeVar, overload, Protocol

X = 1
MAX_N: int = 3
Alias = dict[str, int]
Alias2: TypeAlias = list[int]
U = int | None
T = TypeVar("T")
type Alias3 = int
__all__ = ["X", "f"]
a = b = 2
t1, t2 = 1, 2
try:
    import foo
except ImportError:
    foo = None
if __name__ == "__main__":
    scriptvar = 1


@dataclass
class D(Base, Generic[T]):
    """doc"""
    x: int
    y: str = "a"
    z = 3

    def m(self) -> int: ...

    @property
    def p(self): return 1

    async def am(self): pass


class E(Enum):
    A = 1
    B = "b"


class P(Protocol):
    def q(self) -> int:
        """doc"""
        ...


class S:
    def __init__(self, a, b):
        self.first, self.second = a, b
        self.n: int = 0
        for x in a:
            self.last = x

    def use(self):
        self.n = 2


@overload
def f(x: int) -> int: ...
@overload
def f(x: str) -> str: ...
def f(x): return x
'''


def _symbols() -> dict[str, tuple[str, str, bool, int]]:
    pf = parse_python("pkg/m.py", MODULE)
    return {s.qualified_name: (s.kind, s.signature, s.has_body, s.start_line) for s in pf.symbols}


def test_module_bindings_are_const_and_type_rows() -> None:
    syms = _symbols()
    assert syms["pkg.m::X"][:2] == ("const", "")
    assert syms["pkg.m::MAX_N"][:2] == ("const", "int")
    assert syms["pkg.m::Alias"][:2] == ("type", "dict[str, int]")
    assert syms["pkg.m::Alias2"][:2] == ("type", "list[int]")
    assert syms["pkg.m::U"][:2] == ("type", "int | None")
    assert syms["pkg.m::T"][:2] == ("type", 'TypeVar("T")')
    assert syms["pkg.m::Alias3"][:2] == ("type", "int")
    assert syms["pkg.m::__all__"][:2] == ("const", '["X", "f"]')
    # chained and tuple targets name every identifier; a guard's binding counts
    assert {
        syms["pkg.m::a"][0],
        syms["pkg.m::b"][0],
        syms["pkg.m::t1"][0],
        syms["pkg.m::t2"][0],
    } == {"const"}
    assert syms["pkg.m::foo"][0] == "const"
    # the `if __name__ == "__main__":` block is the script's, not the module's
    assert "pkg.m::scriptvar" not in syms
    # bindings never have a body: no chunk of their own, never a caller
    assert not syms["pkg.m::X"][2]


def test_class_attributes_and_enum_members_are_fields() -> None:
    syms = _symbols()
    assert syms["pkg.m::D::x"] == ("field", "int", False, 27)
    assert syms["pkg.m::D::y"][:2] == ("field", "str")
    assert syms["pkg.m::D::z"][:2] == ("field", "")
    assert syms["pkg.m::E::A"][:2] == ("field", "= 1")
    assert syms["pkg.m::E::B"][:2] == ("field", '= "b"')
    # instance attributes set in methods, tuple targets included, first line wins
    assert syms["pkg.m::S::first"][0] == "field" and syms["pkg.m::S::second"][0] == "field"
    assert syms["pkg.m::S::n"] == ("field", "int", False, 53)
    assert syms["pkg.m::S::last"][0] == "field"
    # methods stay `function` rows (the chunker, the edge walker and the
    # outline key on the kind; the qualified name says it is a method)
    assert syms["pkg.m::D::p"][0] == "function"
    assert syms["pkg.m::D::am"][0] == "function"


def test_stub_bodies_are_declarations_and_overloads_fold() -> None:
    syms = _symbols()
    assert syms["pkg.m::D::m"][2] is False, "a `...` body is a declaration"
    assert syms["pkg.m::P::q"][2] is False, "a docstring then `...` is still a declaration"
    assert syms["pkg.m::D::p"][2] is True
    # three `f` rows (two @overload signatures, one implementation) fold to the implementation
    pf = parse_python("pkg/m.py", MODULE)
    fs = [s for s in pf.symbols if s.qualified_name == "pkg.m::f"]
    assert len(fs) == 1 and fs[0].has_body and fs[0].signature == "(x)"


def test_heritage_reads_a_subscripted_base() -> None:
    pf = parse_python("pkg/m.py", MODULE)
    assert ("pkg.m::D", "Base") in pf.inherits
    assert ("pkg.m::D", "Generic") in pf.inherits
    assert ("pkg.m::E", "Enum") in pf.inherits


def test_a_binding_of_the_name_is_never_what_a_call_binds() -> None:
    src = b"""
timeout = 3
def timeout_user():
    return timeout()
def other():
    return helper()
helper = None
"""
    pf = parse_python("pkg/c.py", src)
    nt: dict[str, list[tuple[str, str, str]]] = {}
    for s in pf.symbols:
        nt.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    es, ms = extract_edges_and_misses(
        pf, gate_name_table(nt, "python"), None, None, {"py_present": {"pkg/c.py"}}
    )
    assert not [e for e in es if e.edge_kind == "calls"]
    assert {m.callee_name for m in ms} == {"timeout", "helper"}


def test_decorator_calls_belong_to_the_decorated_def() -> None:
    src = b"""
import pytest
from pkg.base import make

@pytest.mark.parametrize("v", [make(1), make(2)])
def test_x(v):
    pass

class K:
    @decorate(make(3))
    def m(self):
        pass
"""
    base = parse_python("pkg/base.py", b"def make(n):\n    return n\n")
    pf = parse_python("pkg/t.py", src)
    nt: dict[str, list[tuple[str, str, str]]] = {}
    for s in [*base.symbols, *pf.symbols]:
        nt.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    present = {"pkg/base.py", "pkg/t.py"}
    pf.includes = resolve_imports("pkg/t.py", pf.includes, present)
    es, _ms = extract_edges_and_misses(
        pf, gate_name_table(nt, "python"), None, None, {"py_present": present}
    )
    calls = {
        (e.from_qualified_name, e.to_qualified_name, e.line) for e in es if e.edge_kind == "calls"
    }
    assert ("pkg.t::test_x", "pkg.base::make", 5) in calls
    assert ("pkg.t::K::m", "pkg.base::make", 10) in calls
    # the def's row keeps the `def` line the compiler tier records
    assert next(s.start_line for s in pf.symbols if s.name == "test_x") == 6


def test_routes_commands_decorators_and_async_are_facets() -> None:
    src = b"""
from fastapi import APIRouter, FastAPI
import click
app = FastAPI()
router = APIRouter(prefix="/items")

@router.get("/{item_id}")
async def read_item(item_id: int):
    return {}

@app.api_route("/multi", methods=["GET", "POST"])
def multi(): ...

@app.websocket("/ws")
async def ws(): ...

app.include_router(router, prefix="/api")

@click.group()
def cli(): pass

@cli.command("sync")
def sync_cmd(): pass

@cli.command()
def dump_all(): pass

@functools.lru_cache(maxsize=2)
@property
def cached(self): return 1

sub.add_parser("ingest")
"""
    facets = {(f.kind, f.name, f.line) for f in py_facets(parse_python("svc/api.py", src))}
    assert ("http_route", "GET /api/items/{item_id}", 8) in facets, "prefix + include_router"
    assert ("http_route", "GET /multi", 12) in facets and (
        "http_route",
        "POST /multi",
        12,
    ) in facets
    assert ("http_route", "WS /ws", 15) in facets
    assert ("command", "sync", 23) in facets
    assert ("command", "dump-all", 26) in facets, "click derives the name from the def"
    assert ("command", "cli", 20) in facets
    assert ("command", "ingest", 32) in facets
    assert ("decorator", "functools.lru_cache", 30) in facets
    assert ("decorator", "property", 30) in facets
    assert ("decorator", "router.get", 8) in facets
    assert ("modifier", "async", 8) in facets and ("modifier", "async", 15) in facets
    assert ("modifier", "async", 12) not in facets
    assert all(f.confidence == 1.0 for f in py_facets(parse_python("svc/api.py", src)))


def test_flask_django_and_aiohttp_routes() -> None:
    src = b"""
from flask import Blueprint
from django.urls import path, re_path, include
from aiohttp import web
bp = Blueprint("x", __name__, url_prefix="/admin")
@bp.route("/users", methods=["GET", "POST"])
def users(): pass
@bp.get("/ping")
def ping(): pass
urlpatterns = [
    path("articles/<int:year>/", views.year_archive),
    re_path(r"^bio/(?P<u>\\w+)/$", views.bio),
    path("api/", include("api.urls")),
]
web.get("/health", health)
app.router.add_post("/submit", submit)
app.add_url_rule("/old", view_func=old)
"""
    facets = {(f.kind, f.name) for f in py_facets(parse_python("svc/urls.py", src))}
    routes = {n for k, n in facets if k == "http_route"}
    assert routes == {
        "GET /admin/users",
        "POST /admin/users",
        "GET /admin/ping",
        "ANY /articles/<int:year>/",
        "ANY /bio/(?P<u>\\w+)/",
        "GET /health",
        "POST /submit",
        "ANY /old",
    }, "an `include(...)` mount is a prefix, not a route"


def test_a_file_without_decorators_or_routes_has_no_facets() -> None:
    assert py_facets(parse_python("m.py", b"def f():\n    return 1\nX = 2\n")) == []


def test_a_router_naming_its_class_in_its_arguments_keeps_its_prefix() -> None:
    src = (
        'r = APIRouter(prefix="/v1", route_class=TimedAPIRouter)\n'
        'bp = Blueprint("UserBlueprint", __name__, url_prefix="/u")\n'
    )
    assert [m.group(1) for m in _PREFIX_DECL_RE.finditer(src)] == ["r", "bp"]


def test_a_methods_list_with_a_bracket_inside_gives_the_default_method() -> None:
    # The list's scan stops at the next opener, so one search cannot run to the file's end.
    src = b'@app.route("/a", methods=["GET", ("POST")])\ndef a(): pass\n'
    facets = py_facets(parse_python("svc/a.py", src))
    assert [f.name for f in facets if f.kind == "http_route"] == ["GET /a"]


def test_a_router_prefix_is_cut_however_it_grows() -> None:
    # A route name keeps 200 characters; a prefix joined into every route must not grow.
    from server.code.domain.extract.py_routes import _prefixes

    repeated = "a.include_router(r, prefix='/aaaaaaaaaa')\n" * 100
    declared = "r = APIRouter(prefix='/" + "a" * 1000 + "')\n"
    assert len(_prefixes(repeated)["r"]) == 400
    assert len(_prefixes(declared)["r"]) == 400


def test_a_call_holding_its_own_opener_is_read_from_the_inner_one() -> None:
    # The arguments stop at the next same call, so a search cannot run on to the file's end.
    src = b'import starlette.routing\nroutes = [Route("/a",  # was Route("/old"\n  methods=["POST"])]\n'
    facets = py_facets(parse_python("svc/app.py", src))
    assert [f.name for f in facets if f.kind == "http_route"] == ["POST /old"]


def test_a_member_of_a_module_longer_than_a_spec_is_not_joined() -> None:
    long = ".".join(["a"] * 200)
    pf = parse_python("x.py", f"from {long} import b, c\nfrom m import d\n".encode())
    assert pf.includes == [long, "m", "m.d"]
