"""The registration statement is the caller of the class it hands to a
registry.

Parser tier: `factory.registerFunction<T>(…)`, `registerFunction(name,
createX)`, `{createX, …}`, `createT<Q>`, a lambda that constructs `T`, a
same-file creator that constructs `T` — each becomes `calls` edges from the
enclosing registration function to the class's methods this file defines
(1.0), its `create` / `executeImpl` repo-wide (0.6), its constructor when no
`create` binds, the creator function itself, and the template arguments'
classes (`Impl::apply`). `registerAlias` is not a registration. Nothing here
emits a miss, and every edge the walker bound before is bound the same way.

Read side: `via: registration` on code.callers edges and code.impact rows
whose caller is a registration function; the review brief's
`callers_registration`; every payload byte-identical when nothing applies.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.domain.extract import registration as reg
from server.code.domain.extract.edges_treesitter import (
    collect_call_names,
    extract_edges_and_misses,
)
from server.code.domain.extract.treesitter import parse_cpp, symbol_id

_SRC = b"""\
#include <memory>
namespace DB {
class IFunction { public: virtual int executeImpl() const = 0; };
class FunctionFoo : public IFunction {
public:
    static std::shared_ptr<IFunction> create() { return std::make_shared<FunctionFoo>(); }
    FunctionFoo() = default;
    int executeImpl() const override { return helper(); }
    int getReturnTypeImpl() const { return 1; }
private:
    int helper() const { return 2; }
};
struct AbsImpl { static int apply(int a) { return a < 0 ? -a : a; } };
struct NameAbs { static constexpr auto name = "abs"; };
using FunctionAbs = FunctionUnaryArithmetic<AbsImpl, NameAbs, false>;
class AggregateFunctionUniq {
public:
    AggregateFunctionUniq(int) {}
    void add(int) {}
    void merge() {}
};
std::shared_ptr<AggregateFunctionUniq> createAggregateFunctionUniq(int n)
{
    return std::make_shared<AggregateFunctionUniq>(n);
}
class FunctionLambdaMade { public: void run() {} };
class FunctionCreated { public: static int create(int) { return 1; } void execute() {} };
REGISTER_FUNCTION(Foo)
{
    FunctionDocumentation documentation{"d"};
    factory.registerFunction<FunctionFoo>(documentation);
    factory.registerFunction<FunctionAbs>(documentation, FunctionFactory::Case::Insensitive);
    factory.registerFunction<FunctionElsewhere>();
    factory.registerFunction<FunctionHeaderOnly>();
    factory.registerAlias("foo2", "foo");
}
void registerAggregateFunctionUniq(AggregateFunctionFactory & factory)
{
    AggregateFunctionProperties properties = {.returns_default_when_only_null = true};
    factory.registerFunction("uniq", {createAggregateFunctionUniq, properties}, AggregateFunctionFactory::Case::Insensitive);
    factory.registerFunction(Name::name, createAggregateFunctionQuantile<FuncQuantile>);
    factory.registerFunction("made", [](ContextPtr) { return std::make_shared<FunctionLambdaMade>(); });
    factory.registerFunction("created", [](ContextPtr ctx)
    {
        return FunctionCreated::create(ctx);
    });
}
int notARegistrar()
{
    auto f = std::make_shared<FunctionFoo>();
    registerLater(f);
    return f->executeImpl();
}
}
"""

# the repo-wide table: a class defined in another file with `create`,
# `executeImpl` and two constructors; the template `FunctionUnaryArithmetic`
# with one `executeImpl`; a header-only class with no `create` and one
# constructor; a creator overloaded twice (ambiguous repo-wide)
_TABLE: dict[str, list[tuple[str, str, str]]] = {
    "create": [
        ("e-create", "DB::FunctionElsewhere::create", "function"),
        ("u-create", "DB::FunctionUnaryArithmetic::create", "function"),
        ("o-create", "DB::Other::create", "function"),
    ],
    "executeImpl": [
        ("e-exec", "DB::FunctionElsewhere::executeImpl", "function"),
        ("u-exec", "DB::FunctionUnaryArithmetic::executeImpl", "function"),
    ],
    "FunctionElsewhere": [
        ("e-cls", "DB::FunctionElsewhere", "class"),
        ("e-ctor1", "DB::FunctionElsewhere::FunctionElsewhere", "function"),
        ("e-ctor2", "DB::FunctionElsewhere::FunctionElsewhere", "function"),
    ],
    "FunctionHeaderOnly": [
        ("h-cls", "DB::FunctionHeaderOnly", "class"),
        ("h-ctor", "DB::FunctionHeaderOnly::FunctionHeaderOnly", "function"),
    ],
    "FunctionUnaryArithmetic": [("u-cls", "DB::FunctionUnaryArithmetic", "class")],
    "createAggregateFunctionQuantile": [
        ("q1", "DB::createAggregateFunctionQuantile", "function"),
        ("q2", "DB::createAggregateFunctionQuantile", "function"),
    ],
    "registerLater": [("rl", "DB::registerLater", "function")],
}


def _pf():
    return parse_cpp("src/Functions/foo.cpp", _SRC)


def _reg_edges():
    edges, misses = extract_edges_and_misses(_pf(), _TABLE)
    by_from: dict[str, set[tuple[str, float, int]]] = {}
    for e in edges:
        if e.edge_kind == "calls":
            by_from.setdefault(e.from_qualified_name, set()).add(
                (e.to_qualified_name, e.confidence, e.line)
            )
    return edges, misses, by_from


# ---- sites --------------------------------------------------------------------


def test_sites_name_the_method_the_types_and_the_creators() -> None:
    sites = {s.line: s for s in reg.registration_sites(_pf())}
    assert sites[31].types == ("FunctionFoo",) and sites[31].creators == ()
    assert sites[32].types == ("FunctionAbs",)
    assert 35 not in sites, "registerAlias is not a registration"
    # the brace form: the creator identifier, the registrar's own local excluded
    assert sites[40].creators == ("createAggregateFunctionUniq",)
    # a template creator: its name, and its type argument is a registered class
    assert sites[41].creators == ("createAggregateFunctionQuantile",)
    assert sites[41].types == ("FuncQuantile",)
    # a lambda argument spans lines; the site records the call's span
    assert sites[42].creators == () and sites[42].end_line == 42
    assert (sites[43].line, sites[43].end_line) == (43, 46)
    # `registerLater(f)` is a plain call in a non-registrar: not a registry method form? It is
    # (`register[A-Z]`), but `f` is a local of that function and nothing is constructed
    # by its argument list, so it yields no site worth an edge
    assert sites[51].creators == () and sites[51].types == ()


def test_registration_caller_shape() -> None:
    assert reg.is_registration_caller("DB::REGISTER_FUNCTION")
    assert reg.is_registration_caller("DB::REGISTER_FUNCTION(ArrayConcat)")
    assert reg.is_registration_caller("DB::registerAggregateFunctionUniq")
    assert reg.is_registration_caller("registerStorageMergeTree")
    assert reg.is_registration_caller("DB::registerFunctionsArithmetic(FunctionFactory &)")
    assert not reg.is_registration_caller("DB::FunctionFoo::executeImpl")
    assert not reg.is_registration_caller("DB::register")  # no capital after the verb
    assert not reg.is_registration_caller("DB::Registry::registered")
    assert not reg.is_registration_caller("DB::unregisterFunction")
    assert not reg.is_registration_caller("")


# ---- edges --------------------------------------------------------------------


def test_template_form_binds_every_local_method_and_the_impl() -> None:
    _edges, _misses, by_from = _reg_edges()
    foo = by_from["DB::REGISTER_FUNCTION"]
    # every method FunctionFoo defines here, at 1.0, from the registration line
    for m in ("create", "FunctionFoo", "executeImpl", "getReturnTypeImpl", "helper"):
        assert (f"DB::FunctionFoo::{m}", 1.0, 31) in foo, m
    # the alias `FunctionAbs` -> `FunctionUnaryArithmetic<AbsImpl, …>`: the template's
    # create/executeImpl repo-wide at 0.6, the impl's static method locally at 1.0
    assert ("DB::FunctionUnaryArithmetic::create", 0.6, 32) in foo
    assert ("DB::FunctionUnaryArithmetic::executeImpl", 0.6, 32) in foo
    assert ("DB::AbsImpl::apply", 1.0, 32) in foo
    # a class in another file: create + executeImpl at 0.6; its two constructors are
    # not guessed (create binds, and the overload set is ambiguous anyway)
    assert ("DB::FunctionElsewhere::create", 0.6, 33) in foo
    assert ("DB::FunctionElsewhere::executeImpl", 0.6, 33) in foo
    assert not any(t.startswith("DB::FunctionElsewhere::FunctionElsewhere") for t, _c, _l in foo)
    # a header-only class with no create and one constructor: the constructor
    assert ("DB::FunctionHeaderOnly::FunctionHeaderOnly", 0.6, 34) in foo
    # `Other::create` in the table never binds: it is not FunctionElsewhere's
    assert not any(t.startswith("DB::Other") for t, _c, _l in foo)


def test_creator_forms_bind_the_creator_and_the_class_it_constructs() -> None:
    _edges, _misses, by_from = _reg_edges()
    agg = by_from["DB::registerAggregateFunctionUniq"]
    # the creator handed over inside a brace list, same file: 1.0
    assert ("DB::createAggregateFunctionUniq", 1.0, 40) in agg
    # the class the creator constructs is registered: its methods, not its
    # constructor (the creator's make_shared is that edge)
    assert ("DB::AggregateFunctionUniq::add", 1.0, 40) in agg
    assert ("DB::AggregateFunctionUniq::merge", 1.0, 40) in agg
    assert ("DB::AggregateFunctionUniq::AggregateFunctionUniq", 1.0, 40) not in agg
    assert ("DB::AggregateFunctionUniq::AggregateFunctionUniq", 1.0, 24) in by_from[
        "DB::createAggregateFunctionUniq"
    ]
    # a template creator overloaded twice repo-wide: not guessed
    assert not any(t == "DB::createAggregateFunctionQuantile" for t, _c, _l in agg)
    # a lambda that constructs a class: the class's methods from the registration line
    assert ("DB::FunctionLambdaMade::run", 1.0, 42) in agg
    # a lambda that calls `T::create(...)`: T is registered too (create itself is the
    # lambda's own call edge, bound by the ordinary ladder)
    assert ("DB::FunctionCreated::execute", 1.0, 43) in agg
    # `properties` (a local) never became a creator
    assert not any("properties" in t for t, _c, _l in agg)


def test_a_non_registrar_gets_no_registration_edges_and_the_walker_is_additive() -> None:
    edges, misses, by_from = _reg_edges()
    other = by_from["DB::notARegistrar"]
    # its own calls are what they were: the construction and the plain call
    assert ("DB::FunctionFoo::FunctionFoo", 1.0, 50) in other
    assert ("DB::registerLater", 0.6, 51) in other
    assert not any(t == "DB::FunctionFoo::helper" for t, _c, _l in other)
    # no registration edge ever produced a miss
    assert not [
        m
        for m in misses
        if m.callee_name in {"FunctionFoo", "FunctionElsewhere", "FunctionHeaderOnly", "AbsImpl"}
    ]
    # the factory call's own unresolved miss (the registerFunction<T> template
    # method has no definition here) is untouched — the pass is additive
    assert [m for m in misses if m.callee_name.startswith("registerFunction")]
    # dedupe: one edge per (from, to, line)
    keys = [(e.from_symbol_id, e.to_symbol_id, e.path, e.line) for e in edges]
    assert len(keys) == len(set(keys))


def test_collect_call_names_includes_registered_classes_and_interface() -> None:
    names = collect_call_names(_pf())
    assert {
        "FunctionFoo",
        "FunctionUnaryArithmetic",
        "AbsImpl",
        "NameAbs",
        "FunctionElsewhere",
        "FunctionHeaderOnly",
        "create",
        "executeImpl",
        "createAggregateFunctionUniq",
        "createAggregateFunctionQuantile",
        "FuncQuantile",
    } <= names


def test_without_registration_the_constructor_fixture_is_unchanged() -> None:
    """The constructor-edge fixture has no registry call: the walker's output
    is identical with the module present (nothing but registrations move)."""
    from tests.unit import test_code_ctor_call_edges as ctor_edges

    pf = ctor_edges._pf()
    assert reg.registration_sites(pf) == []
    edges, misses = extract_edges_and_misses(pf, ctor_edges._TABLE)
    assert not any(reg.is_registration_caller(e.from_qualified_name) for e in edges)
    assert len(edges) > 0 and len(misses) > 0


def test_symbol_ids_of_targets_are_the_index_ids() -> None:
    edges, _m, _b = _reg_edges()
    exec_edges = [
        e
        for e in edges
        if e.to_qualified_name == "DB::FunctionFoo::executeImpl"
        and e.from_qualified_name == "DB::REGISTER_FUNCTION"
    ]
    assert exec_edges and exec_edges[0].to_symbol_id == symbol_id(
        "DB::FunctionFoo::executeImpl", "()"
    )


# ---- read side --------------------------------------------------------------------


def _sym(name: str, sid: str, path: str = "src/Functions/foo.cpp") -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "name": name.split("::")[-1],
        "kind": "function",
        "path": path,
        "start_line": 10,
        "end_line": 40,
        "tier": "treesitter",
        "signature": "",
        "doc_comment": "",
    }


def _edge(frm: str, to: str, fq: str, tq: str, line: int = 5) -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": fq,
        "to_qualified_name": tq,
        "path": "src/Functions/foo.cpp",
        "line": line,
        "tier": "treesitter",
        "edge_kind": "calls",
        "confidence": 1.0,
    }


class _Repo:
    def __init__(self) -> None:
        self.symbols = {"DB::FunctionFoo::executeImpl": _sym("DB::FunctionFoo::executeImpl", "sE")}
        self.callers = {
            "sE": [
                _edge("sR", "sE", "DB::REGISTER_FUNCTION", "DB::FunctionFoo::executeImpl", 31),
                _edge("sX", "sE", "DB::useIt", "DB::FunctionFoo::executeImpl", 60),
            ]
        }

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def edges_for_symbols(
        self,
        org,
        ns,
        ids,
        *,
        direction,
        edge_kinds=None,
        limit=200,
        min_confidence=0.0,
        per_match=None,
    ):
        out = []
        for i in ids:
            out.extend(self.callers.get(i, []))
        return out

    async def functions_for_paths(self, org, ns, paths, limit=2000, **_kw):
        return []

    async def live_call_misses_for_names(self, *a, **k):
        return [], False

    async def declined_calls_for_names(self, *a, **k):
        return {}

    async def scip_paths_among(self, *a, **k):
        return set()

    async def state(self, *a, **k):
        return {}

    async def all_state(self, *a, **k):
        return {}

    async def test_map_for(self, *a, **k):
        return []

    async def pr_history_for(self, *a, **k):
        return []

    async def reviewers_by_pr(self, *a, **k):
        return {}

    async def top_reviewers(self, *a, **k):
        return []


@pytest.fixture
def repo(monkeypatch):
    from server.code.app.symbol import graph, impact

    r = _Repo()
    for m in (graph, impact):
        monkeypatch.setattr(m, "CodeRepo", lambda: r)
        monkeypatch.setattr(m, "require_code_intel", lambda: None)
        monkeypatch.setattr(m, "resolve_code_identity", lambda org, ns: ("o", "n"))
        monkeypatch.setattr(m, "schedule_code_trace", lambda **k: None)
        monkeypatch.setattr(m, "_dispatch_seeds", _none_rows)
        monkeypatch.setattr(m, "_declined_safe", _zero_pair)
        monkeypatch.setattr(m, "_scip_coverage", _empty_set)
        monkeypatch.setattr(m, "_dispatch_missing", _false)
    monkeypatch.setattr(graph, "_candidate_block", _none)
    # the walk stays on resolved edges: dispatch widening is not this file's subject
    monkeypatch.setattr(graph, "_widening", lambda _inp: (False, 0.2))
    return r


async def _none_rows(*a, **k):
    return []


async def _zero_pair(*a, **k):
    return (0, 0)


async def _empty_set(*a, **k):
    return set()


async def _false(*a, **k):
    return False


async def _none(*a, **k):
    return None


@pytest.mark.asyncio
async def test_callers_tags_registration_edges_and_only_those(repo) -> None:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol import handle_code_callers

    out = await handle_code_callers(CodeCallersInput(name="DB::FunctionFoo::executeImpl"))
    wire = out.model_dump()
    by_from = {e["from_symbol"]: e for e in wire["edges"]}
    assert by_from["DB::REGISTER_FUNCTION"]["via"] == "registration"
    assert "via" not in by_from["DB::useIt"], "the key rides only where it applies"
    assert "via_override" not in by_from["DB::useIt"]
    assert "resolved_by" not in wire, "an exact hit discloses nothing"
    # epistemic is unchanged by the edge: the seed file is parser-only, so lower_bound
    assert out.epistemic == "lower_bound" and "treesitter_only_files" in out.causes


@pytest.mark.asyncio
async def test_impact_rows_carry_via_only_for_registrars(repo) -> None:
    from server.code.api.models_symbol import CodeImpactInput
    from server.code.app.symbol import handle_code_impact

    out = await handle_code_impact(CodeImpactInput(name="DB::FunctionFoo::executeImpl", depth=1))
    rows = {r["qualified_name"]: r for r in out.impacted_symbols}
    assert rows["DB::REGISTER_FUNCTION"]["via"] == "registration"
    assert "via" not in rows["DB::useIt"]
    assert out.verdict == "" and "resolved_by" not in out.model_dump()


def test_edge_wire_drops_empty_via() -> None:
    from server.code.api.models_symbol import CodeEdgeWire

    base = dict(
        edge_kind="calls",
        from_symbol="a",
        to_symbol="b",
        from_symbol_id="1",
        to_symbol_id="2",
        path="p",
        line=1,
        tier="scip",
        confidence=1.0,
    )
    assert "via" not in CodeEdgeWire(**base).model_dump()
    assert CodeEdgeWire(**base, via="registration").model_dump()["via"] == "registration"
