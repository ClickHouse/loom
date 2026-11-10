"""Tree-sitter parsing for Python — the fast tier's second language.

Produces the same ``ParsedFile``/``Symbol`` shapes as ``parse_cpp``, so the
chunker, edge extractor, and every read path work unchanged. Two Python-
specific decisions, both load-bearing:

1. **Qualified names are module-prefixed.** ``code_symbols`` keys on
   ``(…, qualified_name, symbol_id)`` and ``symbol_id`` hashes
   (qualified_name, signature) — a corpus of test files defines ``main()``
   and pytest fixtures with identical names AND identical ``()`` signatures
   hundreds of times, so path-free names would clobber each other under
   FINAL. The module scope is the repo-relative path with ``/``→``.`` and
   the extension dropped (``tests/integration/test_x/test.py`` →
   ``tests.integration.test_x.test``), joined with the index's universal
   ``::`` separator: ``tests.integration.test_x.test::TestFoo::test_bar``.
   Bare-name lookup (``symbols_by_name("test_bar")``) and suffix matching
   (``endsWith("::test_bar")``) keep working untouched.

2. **The docstring is the doc comment.** Python documents below the
   ``def``, not above it; a leading ``#`` comment block (the C++ heuristic)
   is used only when there is no docstring.
"""

from __future__ import annotations

import functools
import re
from functools import lru_cache
from typing import Any

from server.code.domain.extract import bounded_parse
from server.code.domain.extract.py_members import (
    class_members,
    fold_declarations,
    is_enum_class,
    is_stub_body,
    module_bindings,
)
from server.code.domain.extract.py_receivers import type_name_of
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

_MAX_DOC = 2000

# pytest wiring (see edges_treesitter for the resolution ladder). A fixture
# request is a PARAMETER NAME on a test/fixture — no call expression exists,
# so the call-graph walk can never see it.
_FIXTURE_DEC_RE = re.compile(r"@\s*(?:pytest\.)?fixture\b")
_PARAMETRIZE_DEC_RE = re.compile(r"@\s*(?:pytest\.)?mark\.parametrize|@\s*parametrize\b")
_USEFIXTURES_DEC_RE = re.compile(r"usefixtures\s*\(([^()]*)\)")
_STR_ARG_RE = re.compile(r"""["']([^"']+)["']""")
_GETFIXTURE_RE = re.compile(r"""\.getfixturevalue\(\s*["'](\w+)["']""")
# Parameters pytest never resolves as fixtures.
_NON_FIXTURE_PARAMS = {"self", "cls", "request"}


STUB_SCOPE = "__stub__"
NOTEBOOK_SCOPE = "__notebook__"


def module_scope(path: str) -> str:
    """Repo-relative path → dotted module scope (extension dropped). A stub
    (`foo.pyi`) is its own module `foo.__stub__` and a notebook
    (`nb.ipynb`) `nb.__notebook__`: both parse with the
    Python grammar, and a stub beside its implementation (`foo.pyi` next to
    `foo.py`) would otherwise define the same qualified names under the
    same symbol ids from another path."""
    name = path.rsplit("/", 1)[-1]
    p = path[: path.rfind(".")] if "." in name else path
    scope = p.replace("/", ".")
    if name.endswith(".pyi"):
        return f"{scope}.{STUB_SCOPE}"
    if name.lower().endswith(".ipynb"):
        return f"{scope}.{NOTEBOOK_SCOPE}"
    return scope


@lru_cache(maxsize=1)
def _py_parser() -> Any:
    import tree_sitter
    import tree_sitter_python

    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_python.language()))


def _docstring(body: Any, source: bytes) -> str:
    """First statement of a def/class body when it is a string literal."""
    if body is None or not body.named_children:
        return ""
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children:
        expr = first.named_children[0]
        if expr.type == "string":
            doc = node_text(expr, source).strip("\"' \n")
            return doc[:_MAX_DOC]
    return ""


def _comment_above(node: Any, source: bytes) -> str:
    parts: list[str] = []
    sib = node.prev_named_sibling
    expected_end = node.start_point[0]
    while sib is not None and sib.type == "comment":
        if expected_end - sib.end_point[0] > 2:
            break
        parts.append(node_text(sib, source))
        expected_end = sib.start_point[0]
        sib = sib.prev_named_sibling
    return "\n".join(reversed(parts))[:_MAX_DOC] if parts else ""


def parse_python(path: str, source: bytes) -> ParsedFile:
    """Parse one Python file: module-scoped symbols, imports, inherits."""
    parser = _py_parser()
    tree = bounded_parse.parse(parser, source)
    pf = ParsedFile(path=path, lang="python", tree=tree, source=source)
    mod = module_scope(path)

    def add_symbol(node: Any, scope: tuple[str, ...], kind: str) -> str:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return ""
        name = node_text(name_node, source)
        qname = "::".join((mod, *scope, name))
        params = node.child_by_field_name("parameters")
        sig = node_text(params, source) if params is not None else ""
        body = node.child_by_field_name("body")
        doc = _docstring(body, source) or _comment_above(node, source)
        # the return annotation, so `x = make_client()` types x
        rt = ""
        if kind == "fixture":
            rt = fixture_yield_type(node.child_by_field_name("return_type"), source)
        elif kind != "class":
            rt = type_name_of(node.child_by_field_name("return_type"), source)
        pf.symbols.append(
            Symbol(
                qualified_name=qname,
                name=name,
                kind=kind,
                return_type=rt,
                # Signature disambiguates same-scope overloads in C++; in
                # Python the module scope already makes qnames unique, and
                # keeping the parameter text makes code.symbol output useful.
                signature=sig,
                path=path,
                # the `def` line, where the compiler tier puts it too: the
                # outline folds the tiers on (qualified name, start line);
                # a decorator above belongs to the def in the edge walker
                # (`edges_treesitter._py_decorator_spans`)
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                doc_comment=doc,
                # a `...` body is a declaration (Protocol,
                # abstract, `@overload`, stub), not a unit with code
                has_body=kind == "class" or not is_stub_body(body),
            )
        )
        return qname

    def _param_names(node: Any) -> list[str]:
        """Positional/keyword parameter names of a def, in order."""
        params = node.child_by_field_name("parameters")
        if params is None:
            return []
        out: list[str] = []
        for p in params.named_children:
            if p.type == "identifier":
                out.append(node_text(p, source))
            elif p.type in {"typed_parameter", "default_parameter",
                            "typed_default_parameter"}:
                ident = p.child_by_field_name("name") or next(
                    (c for c in p.named_children if c.type == "identifier"), None
                )
                if ident is not None:
                    out.append(node_text(ident, source))
        return out

    def _record_fixture_requests(
        node: Any, qname: str, decorators: list[str]
    ) -> None:
        """Parameters of tests and fixtures resolve BY NAME to fixture defs
        (pytest's contract); parametrize arg names are data, not fixtures,
        and are excluded. usefixtures marks request with no parameter."""
        name = qname.split("::")[-1]
        is_fixture = any(_FIXTURE_DEC_RE.search(d) for d in decorators)
        if not (is_fixture or name.startswith("test")):
            return
        parametrized: set[str] = set()
        for d in decorators:
            if _PARAMETRIZE_DEC_RE.search(d):
                m = _STR_ARG_RE.search(d)
                if m:
                    parametrized |= {p.strip() for p in m.group(1).split(",")}
        line = node.start_point[0] + 1
        for p in _param_names(node):
            if p in _NON_FIXTURE_PARAMS or p in parametrized or p.startswith("_"):
                continue
            pf.fixture_requests.append((qname, p, line))
        for d in decorators:
            m = _USEFIXTURES_DEC_RE.search(d)
            if m:
                for fx in _STR_ARG_RE.findall(m.group(1)):
                    pf.fixture_requests.append((qname, fx, line))

    includes: dict[str, None] = {}  # ordered, distinct

    def walk(node: Any, scope: tuple[str, ...]) -> None:
        for child in node.named_children:
            t = child.type
            if t == "decorated_definition":
                inner = child.child_by_field_name("definition")
                decs = [
                    node_text(d, source)[:500]
                    for d in child.named_children
                    if d.type == "decorator"
                ]
                if inner is not None:
                    walk_one(inner, scope, decs)
                continue
            walk_one(child, scope, [])

    def walk_one(
        child: Any, scope: tuple[str, ...], decorators: list[str] | None = None
    ) -> None:
        decorators = decorators or []
        t = child.type
        if t == "function_definition":
            is_fixture = any(_FIXTURE_DEC_RE.search(d) for d in decorators)
            qname = add_symbol(child, scope, "fixture" if is_fixture else "function")
            if qname:
                _record_fixture_requests(child, qname, decorators)
            body = child.child_by_field_name("body")
            name_node = child.child_by_field_name("name")
            if body is not None and name_node is not None:
                # nested defs get the enclosing function in their scope
                walk(body, (*scope, node_text(name_node, source)))
            return
        if t == "class_definition":
            qname = add_symbol(child, scope, "class")
            name_node = child.child_by_field_name("name")
            supers = child.child_by_field_name("superclasses")
            bases: list[str] = []
            if qname and supers is not None:
                for b in supers.named_children:
                    if b.type in {"identifier", "attribute"}:
                        base = node_text(b, source)
                        if base and base not in {"object"}:
                            pf.inherits.append((qname, base))
                            bases.append(base)
                    elif b.type == "subscript":
                        # `class X(Generic[T])`, `class Y(Base[int])`: the head names the base
                        head = b.child_by_field_name("value")
                        if head is not None and head.type in {"identifier", "attribute"}:
                            pf.inherits.append((qname, node_text(head, source)))
                            bases.append(node_text(head, source))
            body = child.child_by_field_name("body")
            if body is not None and name_node is not None:
                walk(body, (*scope, node_text(name_node, source)))
            if qname and body is not None:
                # the attributes the class body declares
                taken = {s.name for s in pf.symbols if s.qualified_name.rsplit("::", 1)[0] == qname}
                pf.symbols.extend(class_members(
                    body, source, qname, path, taken, enum=is_enum_class(bases),
                ))
            return
        if t in {"import_statement", "import_from_statement"}:
            # every module an import names is a specifier the
            # ingest resolves against the live paths (`a.b`, `.m`, `..pkg.mod`);
            # `from m import x` also names the submodule `m.x` when x is one
            includes.update(dict.fromkeys(s for s in import_specs(child, source) if s))
            return
        if t in _MODULE_GUARDS:
            # module-level guards (if __name__ == ..., try / except ImportError)
            # still define symbols and import modules, in every branch
            walk(child, scope)
            return

    walk(tree.root_node, ())
    pf.includes = list(includes)
    # the module's constants, type aliases and `__all__`
    # join the defs and classes; a declaration beside its implementation folds
    taken = {s.name for s in pf.symbols if s.qualified_name.count("::") == 1}
    pf.symbols.extend(module_bindings(tree.root_node, source, mod, path, taken))
    pf.symbols[:] = fold_declarations(pf.symbols)

    # request.getfixturevalue("x") — a string-literal fixture request from
    # anywhere in a body; the enclosing definition is the requestor.
    text = source.decode("utf-8", "replace")
    fn_spans = sorted(
        ((s.start_line, s.end_line, s.qualified_name)
         for s in pf.symbols if s.kind in {"function", "fixture"}),
        key=lambda t: (t[0], -t[1]),
    )
    for m in _GETFIXTURE_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        encl = ""
        for s_ln, e_ln, q in fn_spans:
            if s_ln <= line <= e_ln:
                encl = q  # innermost wins: spans sort widest-first per start
            elif s_ln > line:
                break
        if encl:
            pf.fixture_requests.append((encl, m.group(1), line))
    return pf


# ---------------------------------------------------------------------------
# Python imports as bindings and as file edges.
#
# A Python call `json.dumps(x)` names the module the file imported under
# `json`; `from server.core.ch import get_client` binds `get_client` to one module
# of the repository; `from . import m` names a sibling. Resolving a member call
# by its bare attribute name alone would bind `json.dumps` to the repository's
# own `dumps` and let a TypeScript `group` catch a Python `re.Match.group`. The
# functions below give the call ladder its import rung for Python
# (`edges_treesitter._CallResolver.import_bound`) and the ingest its include
# resolution, both pure over the parse and a set of present paths.
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# a joined member repeats its module: a long module times many members is a copy each
_MAX_SPEC_CHARS = 256


def _dotted(node: Any, source: bytes) -> str:
    return node_text(node, source).strip()


def _same_node(a: Any, b: Any) -> bool:
    """Two wrappers of one tree node (the bindings mint a new object per
    access, so `is` never holds)."""
    return a is not None and b is not None and (
        a.start_byte == b.start_byte and a.end_byte == b.end_byte and a.type == b.type
    )


def import_specs(node: Any, source: bytes) -> list[str]:
    """The module specifiers one import statement names, as written:
    `import a.b as c, d` → ['a.b', 'd']; `from a.b import x, y as z` →
    ['a.b', 'a.b.x', 'a.b.y'] (a member may be a submodule; the ingest keeps
    the specs a file satisfies); `from . import m` → ['.', '.m'];
    `from ..p import q` → ['..p', '..p.q']; `from m import *` → ['m']."""
    out: list[str] = []
    if node.type == "import_statement":
        for c in node.named_children:
            if c.type == "dotted_name":
                out.append(_dotted(c, source))
            elif c.type == "aliased_import":
                name = c.child_by_field_name("name") or next(
                    (x for x in c.named_children if x.type == "dotted_name"), None
                )
                if name is not None:
                    out.append(_dotted(name, source))
        return [o for o in out if o]
    if node.type != "import_from_statement":
        return []
    mod_node = node.child_by_field_name("module_name")
    if mod_node is None:
        return []
    module = _dotted(mod_node, source)
    if not module:
        return []
    out.append(module)
    for c in node.named_children:
        if _same_node(c, mod_node):
            continue
        member = ""
        if c.type == "dotted_name":
            member = _dotted(c, source)
        elif c.type == "aliased_import":
            name = c.child_by_field_name("name") or next(
                (x for x in c.named_children if x.type == "dotted_name"), None
            )
            member = _dotted(name, source) if name is not None else ""
        if member and _IDENT_RE.match(member) and len(module) + 1 + len(member) < _MAX_SPEC_CHARS:
            out.append(join_spec(module, member))
    return out


def join_spec(module: str, member: str) -> str:
    """`a.b` + `x` → `a.b.x`; a relative `..p` + `q` → `..p.q`; `.` + `m` → `.m`."""
    if module.endswith("."):
        return module + member
    return f"{module}.{member}"


def py_import_bindings(pf: ParsedFile) -> dict[str, tuple[str, str]]:
    """{local name: (module specifier, member)} for every name an import
    binds in the file: `import a.b` binds `a.b` (and `a`, the package) with
    member ''; `import a.b as c` binds `c` → ('a.b', ''); `from a.b import x
    as y` binds `y` → ('a.b', 'x'); `from . import m` binds `m` → ('.', 'm').
    A star import binds no name (see `py_star_imports`). Later bindings win,
    as at run time."""
    out: dict[str, tuple[str, str]] = {}
    if pf.tree is None:
        return out
    source = pf.source

    def visit(node: Any) -> None:
        for child in node.named_children:
            t = child.type
            if t == "import_statement":
                for c in child.named_children:
                    if c.type == "dotted_name":
                        spec = _dotted(c, source)
                        out[spec] = (spec, "")
                        head = spec.split(".", 1)[0]
                        out.setdefault(head, (head, ""))
                    elif c.type == "aliased_import":
                        name = c.child_by_field_name("name") or next(
                            (x for x in c.named_children if x.type == "dotted_name"), None
                        )
                        alias = c.child_by_field_name("alias") or next(
                            (x for x in reversed(c.named_children) if x.type == "identifier"),
                            None,
                        )
                        if name is not None and alias is not None:
                            out[_dotted(alias, source)] = (_dotted(name, source), "")
            elif t == "import_from_statement":
                mod_node = child.child_by_field_name("module_name")
                module = _dotted(mod_node, source) if mod_node is not None else ""
                if not module:
                    continue
                for c in child.named_children:
                    if _same_node(c, mod_node):
                        continue
                    if c.type == "dotted_name":
                        member = _dotted(c, source)
                        if _IDENT_RE.match(member):
                            out[member] = (module, member)
                    elif c.type == "aliased_import":
                        name = c.child_by_field_name("name") or next(
                            (x for x in c.named_children if x.type == "dotted_name"), None
                        )
                        alias = c.child_by_field_name("alias") or next(
                            (x for x in reversed(c.named_children) if x.type == "identifier"),
                            None,
                        )
                        if name is not None and alias is not None:
                            member = _dotted(name, source)
                            if _IDENT_RE.match(member):
                                out[_dotted(alias, source)] = (module, member)
            elif t in _IMPORT_HOLDERS:
                # `try: import x except ImportError: x = None`, `if TYPE_CHECKING:`,
                # and the import a function performs in its own body (the
                # layering idiom: `from server.core.ch import get_client` inside the
                # handler) — one file-wide table, the later binding winning
                visit(child)

    visit(pf.tree.root_node)
    return out


def py_star_imports(pf: ParsedFile) -> list[str]:
    """The module specifiers of the file's `from m import *` statements."""
    out: list[str] = []
    if pf.tree is None:
        return out
    source = pf.source

    def visit(node: Any) -> None:
        for child in node.named_children:
            if child.type == "import_from_statement":
                if any(c.type == "wildcard_import" for c in child.named_children):
                    mod_node = child.child_by_field_name("module_name")
                    if mod_node is not None:
                        out.append(_dotted(mod_node, source))
            elif child.type in _IMPORT_HOLDERS:
                visit(child)

    visit(pf.tree.root_node)
    return out


# the statements and their clauses the module walk descends: a guard's
# every branch is module level
_MODULE_GUARDS: frozenset[str] = frozenset({
    "if_statement", "elif_clause", "else_clause", "try_statement", "except_clause",
    "finally_clause", "with_statement", "block",
})


# the nodes an import statement may sit under: guards, handlers, and the
# bodies of definitions (a function's local import binds for that function;
# the file-wide table treats it as the file's, which is what a call site of
# that function needs)
_IMPORT_HOLDERS: frozenset[str] = frozenset({
    "if_statement", "try_statement", "block", "else_clause", "elif_clause", "except_clause",
    "finally_clause", "with_statement", "function_definition", "class_definition",
    "decorated_definition", "for_statement", "while_statement", "match_statement",
    "case_clause",
})


def _package_of(from_path: str, level: int) -> str | None:
    """The directory a relative import of `level` dots starts from: the
    importer's package for one dot, its parent for two… None above the
    repository root."""
    d = from_path.rsplit("/", 1)[0] if "/" in from_path else ""
    for _ in range(level - 1):
        if not d:
            return None
        d = d.rsplit("/", 1)[0] if "/" in d else ""
    return d


def roots_for(from_path: str, extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Where an absolute specifier may start, nearest first as `sys.path`
    reads it: the importer's own directory and each ancestor (a test that
    imports its neighbours, a script beside its helpers), the repository
    root, and every `src/` beneath those (the src layout; a monorepo's
    `packages/x/src/x`); then the roots the checkout's manifests declare
    (`py_layouts.Layout.roots_for`). Distinct, in order."""
    out: dict[str, None] = {}
    d = from_path.rsplit("/", 1)[0] if "/" in from_path else ""
    dirs: list[str] = []
    while True:
        dirs.append(d)
        if not d:
            break
        d = d.rsplit("/", 1)[0] if "/" in d else ""
    for d in dirs:
        out.update(dict.fromkeys((d, f"{d}/src" if d else "src")))
    out.update(dict.fromkeys(extra))
    return list(out)


def module_files(stem: str) -> tuple[str, ...]:
    """The files a module stem may be, in the interpreter's order, then the
    stubs (a stub-only module — a C extension's `.pyi` — is the one
    definition the index has)."""
    return (f"{stem}.py", f"{stem}/__init__.py", f"{stem}.pyi", f"{stem}/__init__.pyi")


def import_candidates(
    from_path: str, spec: str, roots: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """The repository paths a specifier may name, in the order the
    interpreter would try them: for `a.b` under each root `<root>/a/b.py`
    then `<root>/a/b/__init__.py` (then the stubs); for `..p.q` from
    `pkg/sub/m.py` `pkg/p/q.py` then `pkg/p/q/__init__.py`. A bare `.` names
    the importer's own package `__init__.py`. Never the importer itself.
    `roots`: the manifest-declared roots for this importer
    (`py_layouts.Layout.roots_for`), tried after the ancestors."""
    spec = spec.strip()
    if not spec:
        return []
    level = len(spec) - len(spec.lstrip("."))
    rel = spec[level:].replace(".", "/")
    if level:
        base = _package_of(from_path, level)
        if base is None:
            return []
        stem = f"{base}/{rel}" if base and rel else (base or rel)
        if rel:
            cands = list(module_files(stem))
        else:
            cands = [f"{stem}/__init__.py" if stem else "__init__.py",
                     f"{stem}/__init__.pyi" if stem else "__init__.pyi"]
        return [c for c in cands if c != from_path]
    out: dict[str, None] = {}
    for root in roots_for(from_path, roots):
        stem = f"{root}/{rel}" if root else rel
        out.update(dict.fromkeys(module_files(stem)))
    out.pop(from_path, None)
    return list(out)


def wanted_paths(
    from_path: str, specs: list[str], roots: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Every candidate path of every specifier (the ingest's presence probe)."""
    out: dict[str, None] = {}
    for spec in specs:
        out.update(dict.fromkeys(import_candidates(from_path, spec, roots)))
    return list(out)


def resolve_import_map(
    from_path: str, specs: list[str], present: set[str], roots: tuple[str, ...] | list[str] = (),
) -> dict[str, str]:
    """{specifier: the first present candidate} for the specifiers a
    repository file satisfies; the others are absent (a library, the
    standard library, a member that is not a submodule)."""
    out: dict[str, str] = {}
    for spec in specs:
        hit = first_present_candidate(from_path, spec, present, roots)
        if hit:
            out[spec] = hit
    return out


def first_present_candidate(
    from_path: str, spec: str, present: set[str], roots: tuple[str, ...] | list[str] = (),
) -> str:
    """The present path an absolute or relative specifier names, or ''.
    The importer's own directory and its ancestors are
    roots only while they are not packages — inside `pydantic/`,
    `import dataclasses` is the standard library's, never the sibling
    `pydantic/dataclasses.py` (implicit relative imports left with Python 2);
    a directory without `__init__.py` (a script's, a test tree's) still is."""
    if spec.startswith("."):
        return next((c for c in import_candidates(from_path, spec, roots) if c in present), "")
    rel = spec.replace(".", "/")
    from server.code.domain.extract.py_layouts import is_vendored

    explicit = set(roots)  # a manifest's root stands whatever it holds
    for root in roots_for(from_path, roots):
        if (
            root and root not in explicit and not is_vendored(root)
            and from_path.startswith(root + "/") and f"{root}/__init__.py" in present
        ):
            # a package the importer sits in is not a sys.path root; a
            # vendored tree is one by convention
            continue
        stem = f"{root}/{rel}" if root else rel
        for c in (f"{stem}.py", f"{stem}/__init__.py", f"{stem}.pyi"):
            if c in present and c != from_path:
                return c
    return ""


_YIELD_HEADS: frozenset[str] = frozenset({
    "Iterator", "Iterable", "Generator", "AsyncIterator", "AsyncIterable", "AsyncGenerator",
})


def fixture_yield_type(node: Any, source: bytes) -> str:
    """The type a pytest fixture hands its requestors —
    `-> Client`, or the element of `-> Iterator[Client]` / `Generator[Client,
    None, None]` for a yield fixture; '' when the annotation says nothing."""
    if node is None:
        return ""
    direct = type_name_of(node, source)
    if direct:
        return direct
    n = node
    if n.type == "type" and n.named_children:
        n = n.named_children[0]
    if n.type in {"generic_type", "subscript"} and n.named_children:
        head = node_text(n.named_children[0], source).rsplit(".", 1)[-1]
        if head in _YIELD_HEADS:
            args = [c for c in n.named_children[1:]]
            if args and args[0].type == "type_parameter":
                args = list(args[0].named_children)
            return type_name_of(args[0], source) if args else ""
    return ""


def resolve_imports(
    from_path: str, specs: list[str], present: set[str], roots: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """The indexed paths the specifiers name, in import order, one per
    specifier that a present path satisfies (`ingest._resolve_lane_includes`)."""
    return list(dict.fromkeys(resolve_import_map(from_path, specs, present, roots).values()))


def external_names(
    pf: ParsedFile, resolved: set[str], roots: tuple[str, ...] | list[str] = (),
) -> set[str]:
    """The local names whose module no indexed path satisfied — the standard
    library's and the dependencies'. `resolved` is the file's resolved
    include set (paths). A relative specifier always names the repository."""
    out: set[str] = set()
    for local, (spec, _member) in py_import_bindings(pf).items():
        if spec.startswith("."):
            continue
        if not any(c in resolved for c in import_candidates(pf.path, spec, roots)):
            out.add(local)
    return out


def def_paths_of_scope(scope: str) -> tuple[str, str]:
    """The two paths a module-scoped qualified name's scope can come from:
    `a.b` → (`a/b.py`, `a/b/__init__.py`); `a.b.__init__` → (`a/b/__init__.py`,
    same); a stub scope `a.b.__stub__` → the `.pyi` forms; a notebook scope
    `n.__notebook__` → (`n.ipynb`, same). Tells a Python definition apart
    from another script language's under the shared qualified-name scheme."""
    ext = ".py"
    if scope.endswith(f".{STUB_SCOPE}"):
        scope, ext = scope[: -len(STUB_SCOPE) - 1], ".pyi"
    elif scope.endswith(f".{NOTEBOOK_SCOPE}"):
        p = scope[: -len(NOTEBOOK_SCOPE) - 1].replace(".", "/") + ".ipynb"
        return p, p
    if scope.endswith(".__init__"):
        p = scope[: -len(".__init__")].replace(".", "/") + f"/__init__{ext}"
        return p, p
    stem = scope.replace(".", "/")
    return f"{stem}{ext}", f"{stem}/__init__{ext}"


_REEXPORT_DEPTH = 4


@functools.lru_cache(maxsize=2048)
def _reexport_facts(
    path: str, text: str,
) -> tuple[frozenset[str], dict[str, tuple[str, str]], tuple[str, ...]]:
    """What `py_reexport_target` reads off one module's text — its own
    module-level names, its import bindings and its star-import specifiers —
    parsed once per (path, text). The chain is followed once per call site, so
    without the cache a package's `__init__` is re-parsed hundreds of times per
    file (pandas: 1,412 re-parses for 150 files; the ingest prefetches the same
    texts per file). A module whose parse runs past its limits reads as
    defining and importing nothing, and is not parsed again while it stays
    in the cache."""
    try:
        pf = parse_python(path, text.encode("utf-8", "replace"))
    except bounded_parse.ParseTimeout:
        return frozenset(), {}, ()
    own = frozenset(s.name for s in pf.symbols if s.qualified_name.count("::") == 1)
    return own, dict(py_import_bindings(pf)), tuple(py_star_imports(pf))


def py_reexport_target(
    init_path: str, member: str, texts: dict[str, str], present: set[str],
) -> tuple[str, str] | list[str] | None:
    """Where a module gets the name it re-exports (the Python form of the
    TypeScript barrel chains; any module, not only a package's
    `__init__.py` — `sqlalchemy.sql.expression` re-exports `and_`): `from
    .param_functions import Depends as Depends` →
    ('fastapi/param_functions.py', 'Depends'), followed through further
    modules up to `_REEXPORT_DEPTH` hops over `texts` ({path: source}). A
    member reached only through star re-exports
    (`from .a import *`) yields the list of module paths the stars name, for
    the caller to match a definition in. None when the text is unknown or
    the package does not import the name."""
    seen: set[tuple[str, str]] = set()
    path, name = init_path, member
    for _ in range(_REEXPORT_DEPTH):
        if (path, name) in seen or path not in texts:
            return None
        seen.add((path, name))
        own, bindings, star_specs = _reexport_facts(path, texts[path])
        if name in own:
            return path, name  # the package defines it itself
        b = bindings.get(name)
        if b is None:
            # the star modules that exist, by the same
            # package-aware ladder a real import takes (the importer's own
            # directory is not a root inside a package)
            found = (first_present_candidate(path, spec, present) for spec in star_specs)
            stars = [c for c in found if c]
            return stars or None
        spec, imported = b
        if imported == "":
            # `from . import sub as name` / `import a.b as name`: a module, not
            # a definition — the caller resolves member calls on it
            hit = first_present_candidate(path, spec, present)
            return (hit, "") if hit else None
        hit = first_present_candidate(path, join_spec(spec, imported), present)
        if hit:
            return hit, ""  # `from .pkg import sub`: the name is a submodule
        hit = first_present_candidate(path, spec, present)
        if not hit:
            return None
        if hit not in texts:
            return hit, imported  # a module whose text is unknown: its own definition, or none
        path, name = hit, imported  # any module may re-export; keep following
    return None
