"""Tree-sitter parsing for TypeScript and JavaScript.

Same ``ParsedFile`` / ``Symbol`` shapes as ``parse_cpp`` / ``parse_python``,
so the chunker, the edge extractor and every read path work unchanged.
The tree-sitter-typescript grammar (``typescript`` for `.ts` / `.mts` /
`.cts`, ``tsx`` for `.tsx`) and tree-sitter-javascript (`.js` / `.jsx` /
`.mjs` / `.cjs`) share the node vocabulary this module reads.

What a TypeScript file defines, in the order a reader meets it:

* **Declarations** — ``function`` / ``class`` / ``interface`` / ``type`` /
  ``enum`` / ``namespace``, exported or not. Qualified names are
  module-scoped like Python's (`lang_python.module_scope`: the repo path
  with `/` → `.` and the extension dropped) because a monorepo defines
  ``handler``, ``Props`` and ``default`` in hundreds of files, and
  ``code_symbols`` keys on ``(qualified_name, symbol_id)``.
* **Const-bound functions** — ``export const f = async (…) => …`` is how
  most of a Next.js / tRPC codebase writes its functions; the declarator
  is a ``function`` symbol whose span is the whole statement (the
  ``export const`` line included, so the chunk reads as written).
* **Function-valued properties** — a property of an object literal whose
  value is a function, or a call chain that takes one
  (``byId: procedure.input(…).query(async ({ input }) => …)``), is a
  ``function`` symbol under the container's name: a tRPC router's
  procedures are ``routers.observations::observationsRouter::byId``, a
  Next.js API route's handler is ``api.public.events::default::POST::fn``.
  Nothing tRPC-specific is encoded — the rule is the language's, and it
  yields the same for Express routers, Zod ``.transform(fn)`` and
  ``vi.mock(() => …)``.
* **Const containers** — an exported or multi-line ``const`` whose value
  is an object, array or call (a Zod schema, a router, a config table) is
  a ``const`` symbol: it gets a chunk of its own (with function-valued
  members elided, like a class skeleton) and answers ``code.symbol``.
* **Imports** — every ``import … from "x"``, ``export … from "x"``,
  ``require("x")`` and ``import("x")`` specifier is an include target.
  Specifiers are resolved to repository paths by ``resolve_imports`` with
  the set of indexed paths as the oracle (relative paths with the
  compiler's extension probing, ``@/`` / ``~/`` root aliases against the
  nearest ancestor that holds the file, workspace packages by name under
  ``packages/`` and the repository root with their ``src/index``
  convention); an unresolvable specifier (``react``, ``zod``) is not an
  edge.
* **Inheritance** — ``extends`` and ``implements`` clauses are
  ``inherits`` edges to the class or interface they name, bound on the
  call ladder (same file, import, unique repo-wide).
* **The doc comment** is the JSDoc block or ``//`` run right above the
  statement (above the ``export`` keyword when there is one).

The parser is outside the extractor stamp with the other lane modules
(see ``tests/unit/test_code_extractor_stamp.py``): a file no parser touched
before has no rows to heal, and a fix here is applied by ``loom code
backfill --paths-file`` over the TypeScript tree of a namespace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_typescript_nodes import (
    _TS_GRAMMAR_BY_EXT,
    _callee_root,
    _chain_function,
    _comment_above,
    _first_string_arg,
    _grammar_for,
    _is_function_like,
    _key_text,
    _parser,
    _return_type_of,
    _signature,
    _slug,
    _statements_under,
    _unwrap,
    _wrapped_function,
)
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

_HERITAGE_BASE_TYPES = frozenset(
    {
        "identifier",
        "type_identifier",
        "member_expression",
        "generic_type",
        "nested_type_identifier",
    }
)

LANG_TS = "typescript"
LANG_JS = "javascript"
KIND_CONST = "const"
KIND_INTERFACE = "interface"
KIND_TYPE = "type"
KIND_ENUM = "enum"
KIND_FIELD = "field"  # an enum member or a literal-valued key of a const object
KIND_MODULE = "module"
KIND_SUITE = "suite"

# Test-runner blocks (vitest / jest / mocha / node:test), read the way bats tests are
# read: a `describe("d", fn)` is a `suite` symbol named by its description's slug, an
# `it("d", fn)` / `test("d", fn)` a `function` symbol `test_<slug>` whose signature
# keeps the description as written, a hook (`beforeEach(fn)`) a `function` named after
# itself; every one is scoped under the suites that hold it. `it.each(rows)("d %s", fn)`
# and `describe.skip` / `.only` / `.concurrent` are the same blocks.
_SUITE_CALLEES: frozenset[str] = frozenset({"describe", "suite", "context"})
_TEST_CALLEES: frozenset[str] = frozenset({"it", "test", "bench", "specify"})
_HOOK_CALLEES: frozenset[str] = frozenset(
    {
        "beforeEach",
        "afterEach",
        "beforeAll",
        "afterAll",
        "before",
        "after",
        "setup",
        "teardown",
    }
)
_MOCK_CALLEES: frozenset[str] = frozenset({"vi.mock", "jest.mock", "vi.doMock", "jest.doMock"})

_JS_EXTS: frozenset[str] = frozenset({".js", ".jsx", ".mjs", ".cjs"})
# extension probing order of the TypeScript compiler / bundlers
IMPORT_EXTS: tuple[str, ...] = (
    ".ts",
    ".tsx",
    ".d.ts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".vue",  # A single-file component is imported by its own extension
    ".svelte",
    ".astro",
)
_INDEX_FILES: tuple[str, ...] = tuple("index" + e for e in IMPORT_EXTS)
_ROOT_ALIASES: tuple[str, ...] = ("@/", "~/", "#/")
_WORKSPACE_DIRS: tuple[str, ...] = ("packages/", "", "apps/", "libs/")
_MAX_CANDIDATES_PER_IMPORT = 96
_MAX_ALIAS_DEPTH = 4  # `@/` roots at a package (`web/`, `packages/shared/`), 1–2 levels deep

_DECLARATION_TYPES: frozenset[str] = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "lexical_declaration",
        "variable_declaration",
        "internal_module",
        "module",
        "ambient_declaration",
        "function_signature",
    }
)
# CommonJS methods live on the prototype
_PROTOTYPE_MEMBER_RE = re.compile(r"^(\w+)\.prototype\.(\w+)$")
_PROTOTYPE_OBJECT_RE = re.compile(r"^(\w+)\.prototype$")
_STATIC_MEMBER_RE = re.compile(r"^([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)$")
_REQUIRE_RE = re.compile(r"""\brequire\(\s*["']([^"']+)["']\s*\)""")
_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\(\s*["']([^"']+)["']\s*\)""")


def lang_for_ts_path(path: str) -> str:
    """``typescript`` / ``javascript`` for the extensions this module reads,
    '' otherwise. `.d.ts` is TypeScript (a declaration file defines types)."""
    lower = path.lower()
    dot = lower.rfind(".")
    ext = lower[dot:] if dot >= 0 else ""
    if ext in _TS_GRAMMAR_BY_EXT:
        return LANG_TS
    if ext in _JS_EXTS:
        return LANG_JS
    return ""


@dataclass
class _Walker:
    pf: ParsedFile
    source: bytes
    mod: str
    imports: list[str] = field(default_factory=list)
    _names: dict[str, int] = field(default_factory=dict)
    # The local names a later `export { X, Y as Z }` clause or `export default X`
    # exports — a declaration of one is exported
    exported: set[str] = field(default_factory=set)

    # ------------------------------------------------------------ symbols
    def _add(
        self,
        scope: tuple[str, ...],
        name: str,
        kind: str,
        signature: str,
        span: Any,
        doc: str = "",
        has_body: bool = True,
        fn: Any = None,
    ) -> str:
        qname = "::".join((self.mod, *scope, name))
        # a second `const x` in the same scope (block-scoped redeclarations
        # in tests) keeps its own row rather than clobbering the first
        seen = self._names.get(f"{qname}|{signature}", 0)
        self._names[f"{qname}|{signature}"] = seen + 1
        if seen:
            name = f"{name}#{seen + 1}"
            qname = "::".join((self.mod, *scope, name))
        self.pf.symbols.append(
            Symbol(
                return_type=_return_type_of(fn, self.source, doc) if fn is not None else "",
                qualified_name=qname,
                name=name,
                kind=kind,
                signature=signature,
                path=self.pf.path,
                start_line=span.start_point[0] + 1,
                end_line=span.end_point[0] + 1,
                doc_comment=doc,
                has_body=has_body,
            )
        )
        return qname

    # ------------------------------------------------------------ program
    def walk_program(self, root: Any) -> None:
        for child in root.named_children:
            self._statement(child, ())

    def _statement(self, node: Any, scope: tuple[str, ...]) -> None:
        t = node.type
        if t == "import_statement":
            self._import_source(node)
        elif t == "export_statement":
            self._export(node, scope)
        elif t in _DECLARATION_TYPES:
            self._declaration(node, scope, _comment_above(node, self.source), node)
        elif t == "expression_statement":
            self._expression_statement(node, scope)

    def _import_source(self, node: Any) -> None:
        src = node.child_by_field_name("source")
        if src is None:
            src = next((c for c in node.named_children if c.type == "string"), None)
        if src is not None:
            spec = node_text(src, self.source).strip("\"'`")
            if spec:
                self.imports.append(spec)

    def _export(self, node: Any, scope: tuple[str, ...]) -> None:
        if node.child_by_field_name("source") is not None or any(
            c.type == "string" for c in node.named_children
        ):
            self._import_source(node)  # `export … from "x"` re-export
            return
        doc = _comment_above(node, self.source)
        decl = node.child_by_field_name("declaration")
        if decl is not None:
            self._declaration(decl, scope, doc, node)
            return
        value = node.child_by_field_name("value")
        if value is None:
            value = next(
                (
                    c
                    for c in node.named_children
                    if c.type not in {"export_clause", "comment", "decorator"}
                ),
                None,
            )
        if value is not None and value.type not in {"export_clause"}:
            self._default_export(value, scope, doc, node)

    def _default_export(self, value: Any, scope: tuple[str, ...], doc: str, stmt: Any) -> None:
        value = _unwrap(value)
        if value.type in {"class", "class_declaration"}:
            self._class(value, scope, doc, stmt, name="default")
        elif _is_function_like(value) or value.type == "function_declaration":
            name = value.child_by_field_name("name")
            self._add(
                scope,
                node_text(name, self.source) if name is not None else "default",
                "function",
                _signature(value, self.source),
                stmt,
                doc,
            )
        elif value.type in {"object", "call_expression", "array", "new_expression"}:
            self._add(scope, "default", KIND_CONST, "", stmt, doc)
            self._value(value, (*scope, "default"))

    def _expression_statement(self, node: Any, scope: tuple[str, ...]) -> None:
        """`describe(…)` / `it(…)` test blocks, `module.exports = {…}`,
        `exports.f = …`."""
        expr = node.named_children[0] if node.named_children else None
        if expr is None:
            return
        body = _iife_body(expr)
        if body is not None:
            # `(function () { … })()` / `(async () => { … })()` at statement level is
            # the module's own scope, not a call to index
            for st in body.named_children:
                self._statement(st, scope)
            return
        if expr.type == "call_expression":
            self._call_statement(expr, scope, node)
            return
        if expr.type != "assignment_expression":
            return
        left = expr.child_by_field_name("left")
        right = _unwrap(expr.child_by_field_name("right"))
        if left is None or right is None:
            return
        target = node_text(left, self.source)
        if target == "module.exports":
            if _is_function_like(right):
                self._add(
                    scope, "default", "function", _signature(right, self.source), node, fn=right
                )
            else:
                self._add(scope, "default", KIND_CONST, "", node)
                self._value(right, (*scope, "default"))
        elif target.startswith(("exports.", "module.exports.")) and _is_function_like(right):
            self._add(
                scope,
                target.rsplit(".", 1)[-1],
                "function",
                _signature(right, self.source),
                node,
                _comment_above(node, self.source),
                fn=right,
            )
        elif (m := _PROTOTYPE_MEMBER_RE.match(target)) is not None:
            # `Reply.prototype.send = function (payload) {…}` is the CommonJS method —
            # `module::Reply::send`, the unit its calls attribute to and the target
            # `reply.send()` binds to. Kind `function`, as a class method's is: the call
            # ladder's `enclosing()` and the chunker's function chunks key on it.
            if _is_function_like(right):
                self._add(
                    (*scope, m.group(1)),
                    m.group(2),
                    "function",
                    _signature(right, self.source),
                    node,
                    _comment_above(node, self.source),
                    fn=right,
                )
            elif right.type == "object":
                # `Reply.prototype = { send () {}, … }`: the members are the methods
                self._value(right, (*scope, m.group(1)))
        elif (m := _PROTOTYPE_OBJECT_RE.match(target)) is not None and right.type == "object":
            self._value(right, (*scope, m.group(1)))
        elif (m := _STATIC_MEMBER_RE.match(target)) is not None and m.group(1) in self._owners():
            # `PostItem.Skeleton = function () {…}` — a compound component's member
            # (React) or a static helper on a function — is the unit
            # `PostItem::Skeleton`, the target `<PostItem.Skeleton />` binds to
            if _is_function_like(right):
                self._add(
                    (*scope, m.group(1)),
                    m.group(2),
                    "function",
                    _signature(right, self.source),
                    node,
                    _comment_above(node, self.source),
                    fn=right,
                )
            elif right.type == "object":
                self._value(right, (*scope, m.group(1)))

    def _call_statement(self, call: Any, scope: tuple[str, ...], stmt: Any) -> None:
        """A statement that is a call taking a function: a test block, a
        hook, a module mock. Nothing else at statement level is a symbol —
        `app.listen(3000)` defines nothing."""
        root, callee = _callee_root(call, self.source)
        fn = _chain_function(call)
        if fn is None or not root:
            return
        first = _first_string_arg(call, self.source)
        doc = _comment_above(stmt, self.source)
        sig = _signature(fn, self.source)
        if root in _SUITE_CALLEES:
            name = _slug(first) or f"suite_L{stmt.start_point[0] + 1}"
            sig = f'{callee}("{first}")' if first else callee
            self._add(scope, name, KIND_SUITE, sig, stmt, doc)
            body = fn.child_by_field_name("body")
            for c in body.named_children if body is not None else []:
                self._statement(c, (*scope, name))
        elif root in _TEST_CALLEES or callee.rsplit(".", 1)[-1] in _TEST_CALLEES:
            name = "test_" + (_slug(first) or f"L{stmt.start_point[0] + 1}")
            self._add(scope, name, "function", f'{callee}("{first}")' if first else sig, stmt, doc)
            # Node:test / tap nest `await t.test('…', fn)` inside a test; each is a unit
            # of its own under the parent, so a call in it attributes there and "callers
            # of X" names the block that ran
            self._nested_tests(fn, (*scope, name))
        elif root in _HOOK_CALLEES:
            self._add(scope, root, "function", sig, stmt, doc)
        elif callee in _MOCK_CALLEES:
            name = "mock_" + (_slug(first) or f"L{stmt.start_point[0] + 1}")
            self._add(scope, name, "function", f'{callee}("{first}")', stmt, doc)

    def _nested_tests(self, fn: Any, scope: tuple[str, ...]) -> None:
        """Test blocks nested in a test's body (`await t.test(…)`, `it(…)`
        inside `describe`-less trees), at any statement depth that is not
        another function's — recursively."""
        body = fn.child_by_field_name("body")
        if body is None:
            return
        for stmt in _statements_under(body):
            expr = stmt.named_children[0] if stmt.named_children else None
            expr = _unwrap(expr)
            if expr is None or expr.type != "call_expression":
                continue
            root, callee = _callee_root(expr, self.source)
            if callee.rsplit(".", 1)[-1] not in _TEST_CALLEES and root not in _SUITE_CALLEES:
                continue
            inner = _chain_function(expr)
            if inner is None:
                continue
            first = _first_string_arg(expr, self.source)
            name = "test_" + (_slug(first) or f"L{stmt.start_point[0] + 1}")
            self._add(
                scope,
                name,
                "function",
                f'{callee}("{first}")' if first else "",
                stmt,
                _comment_above(stmt, self.source),
            )
            self._nested_tests(inner, (*scope, name))

    # -------------------------------------------------------- declarations
    def _declaration(self, node: Any, scope: tuple[str, ...], doc: str, stmt: Any) -> None:
        t = node.type
        if t in {"function_declaration", "generator_function_declaration"}:
            name = node.child_by_field_name("name")
            if name is not None:
                self._add(
                    scope,
                    node_text(name, self.source),
                    "function",
                    _signature(node, self.source),
                    stmt,
                    doc,
                    fn=node,
                )
        elif t == "function_signature":
            name = node.child_by_field_name("name")
            if name is not None:
                self._add(
                    scope,
                    node_text(name, self.source),
                    "function",
                    _signature(node, self.source),
                    stmt,
                    doc,
                    has_body=False,
                    fn=node,
                )
        elif t in {"class_declaration", "abstract_class_declaration"}:
            self._class(node, scope, doc, stmt)
        elif t == "interface_declaration":
            self._named(node, scope, KIND_INTERFACE, doc, stmt)
            self._interface_members(node, scope)
        elif t == "type_alias_declaration":
            self._named(node, scope, KIND_TYPE, doc, stmt)
        elif t == "enum_declaration":
            self._named(node, scope, KIND_ENUM, doc, stmt)
            self._enum_members(node, scope)
        elif t in {"lexical_declaration", "variable_declaration"}:
            self._variables(node, scope, doc, stmt)
        elif t in {"internal_module", "module"}:
            self._namespace(node, scope, doc, stmt)
        elif t == "ambient_declaration":
            for c in node.named_children:
                if c.type in _DECLARATION_TYPES:
                    self._declaration(c, scope, doc, stmt)

    def _named(self, node: Any, scope: tuple[str, ...], kind: str, doc: str, stmt: Any) -> None:
        name = node.child_by_field_name("name")
        if name is not None:
            self._add(scope, node_text(name, self.source), kind, "", stmt, doc)

    def _interface_members(self, node: Any, scope: tuple[str, ...]) -> None:
        """An interface's method signatures are body-less function units under the
        interface, so a receiver typed by the interface binds its calls and an
        implementing class's methods override them."""
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            return
        inner = (*scope, node_text(name, self.source))
        for m in body.named_children:
            if m.type != "method_signature":
                continue
            key = m.child_by_field_name("name")
            if key is None or key.type not in _MEMBER_KEY_TYPES:
                continue
            nm = node_text(key, self.source).strip("'\"`")
            if nm:
                self._add(inner, nm, "function", _signature(m, self.source), m, "", has_body=False)

    def _enum_members(self, node: Any, scope: tuple[str, ...]) -> None:
        """Every member of an enum is a `field` under the enum, so `Queue.TraceUpsert`
        reads have a row to bind to."""
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            return
        inner = (*scope, node_text(name, self.source))
        for m in body.named_children:
            key = m.child_by_field_name("name") if m.type == "enum_assignment" else m
            if key is not None and key.type in _MEMBER_KEY_TYPES:
                nm = node_text(key, self.source).strip("'\"`")
                if nm:
                    self._add(inner, nm, KIND_FIELD, "", m, "", has_body=False)

    def _namespace(self, node: Any, scope: tuple[str, ...], doc: str, stmt: Any) -> None:
        name = node.child_by_field_name("name")
        if name is None:
            return
        nm = node_text(name, self.source)
        self._add(scope, nm, KIND_MODULE, "", stmt, doc)
        body = node.child_by_field_name("body")
        for c in body.named_children if body is not None else []:
            self._statement(c, (*scope, nm))

    def _class(
        self,
        node: Any,
        scope: tuple[str, ...],
        doc: str,
        stmt: Any,
        name: str = "",
        bound: bool = False,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is not None and not bound:
            name = node_text(name_node, self.source)  # `bound`: `const Foo = class Bar` is Foo
        if not name:
            return
        heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
        qname = self._add(scope, name, "class", "", stmt, doc)
        for clause in heritage.named_children if heritage is not None else []:
            # TypeScript wraps bases in extends_clause / implements_clause; the
            # JavaScript grammar puts the one base straight under class_heritage
            bases = clause.named_children if clause.type.endswith("_clause") else [clause]
            for base in bases:
                if base.type in _HERITAGE_BASE_TYPES:
                    base_text = node_text(base, self.source).split("<", 1)[0]
                    self.pf.inherits.append((qname, base_text.replace(".", "::")))
        body = node.child_by_field_name("body")
        for member in body.named_children if body is not None else []:
            self._class_member(member, (*scope, name))

    def _owners(self) -> set[str]:
        """Module-level names a static member may hang on."""
        return {
            s.name
            for s in self.pf.symbols
            if s.kind in {"function", "class", "const"} and s.qualified_name.count("::") == 1
        }

    def _class_member(self, member: Any, scope: tuple[str, ...]) -> None:
        t = member.type
        span = _with_decorators(member)  # `@Get()` belongs to the method
        if t in {"method_definition", "abstract_method_signature", "method_signature"}:
            name = member.child_by_field_name("name")
            if name is not None:
                self._add(
                    scope,
                    _key_text(name, self.source),
                    "function",
                    _signature(member, self.source),
                    span,
                    _comment_above(span, self.source),
                    has_body=member.child_by_field_name("body") is not None,
                    fn=member,
                )
        elif t == "public_field_definition":
            name = member.child_by_field_name("name")
            value = _unwrap(member.child_by_field_name("value"))
            if name is not None and value is not None and _is_function_like(value):
                self._add(
                    scope,
                    _key_text(name, self.source),
                    "function",
                    _signature(value, self.source),
                    span,
                    _comment_above(span, self.source),
                    fn=value,
                )

    def _variables(self, node: Any, scope: tuple[str, ...], doc: str, stmt: Any) -> None:
        exported_stmt = stmt.type == "export_statement"
        declarators = [c for c in node.named_children if c.type == "variable_declarator"]
        for decl in declarators:
            name = decl.child_by_field_name("name")
            if name is None or name.type != "identifier":
                continue  # destructuring patterns name several things at once
            nm = node_text(name, self.source)
            exported = exported_stmt or nm in self.exported
            value = _unwrap(decl.child_by_field_name("value"))
            span = stmt if len(declarators) == 1 else decl
            if value is None:
                if exported:
                    self._add(scope, nm, KIND_CONST, "", span, doc)
                continue
            if _is_function_like(value):
                self._add(
                    scope, nm, "function", _signature(value, self.source), span, doc, fn=value
                )
                continue
            if value.type in {"class", "class_declaration"}:
                # A class expression is the class its binding names
                self._class(value, scope, doc, span, name=nm, bound=True)
                continue
            wrapped = _wrapped_function(value)
            if wrapped is not None:
                self._add(
                    scope, nm, "function", _signature(wrapped, self.source), span, doc, fn=wrapped
                )
                continue
            multi_line = span.end_point[0] > span.start_point[0]
            if value.type in {
                "object",
                "array",
                "call_expression",
                "new_expression",
                "template_string",
            } and (exported or multi_line):
                self._add(scope, nm, KIND_CONST, "", span, doc)
                self._value(value, (*scope, nm))
            elif exported:
                self._add(scope, nm, KIND_CONST, "", span, doc)

    # ------------------------------------------------------------- values
    def _value(self, node: Any, scope: tuple[str, ...]) -> None:
        """Function-valued members under a container's scope."""
        node = _unwrap(node)
        if node is None:
            return
        if node.type == "object":
            for member in node.named_children:
                if member.type == "pair" and _is_literal(member.child_by_field_name("value")):
                    # A literal-valued key of a const object is a `field` — the
                    # enum-like `Events.UserCreated = "user.created"`
                    key = member.child_by_field_name("key")
                    nm = node_text(key, self.source).strip("'\"`") if key is not None else ""
                    if nm and key is not None and key.type in _MEMBER_KEY_TYPES:
                        self._add(scope, nm, KIND_FIELD, "", member, "", has_body=False)
                    continue
                self._object_member(member, scope)
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                obj = fn.child_by_field_name("object")
                if obj is not None and obj.type == "call_expression":
                    self._value(obj, scope)  # chained: walk the inner calls too
            args = node.child_by_field_name("arguments")
            for arg in args.named_children if args is not None else []:
                if _unwrap(arg).type in {"object", "array", "call_expression"}:
                    self._value(arg, scope)
        elif node.type == "array":
            for el in node.named_children:
                if _unwrap(el).type in {"object", "call_expression"}:
                    self._value(el, scope)

    def _object_member(self, member: Any, scope: tuple[str, ...]) -> None:
        t = member.type
        if t == "method_definition":
            self._class_member(member, scope)
            return
        if t != "pair":
            return
        key = _key_text(member.child_by_field_name("key"), self.source)
        value = _unwrap(member.child_by_field_name("value"))
        if not key or value is None:
            return
        doc = _comment_above(member, self.source)
        if _is_function_like(value):
            self._add(scope, key, "function", _signature(value, self.source), member, doc, fn=value)
        elif value.type == "call_expression":
            fn = _chain_function(value)
            if fn is not None:
                self._add(scope, key, "function", _signature(fn, self.source), member, doc, fn=fn)
            else:
                self._value(value, (*scope, key))
        elif value.type in {"object", "array"}:
            self._value(value, (*scope, key))


def parse_typescript(path: str, source: bytes) -> ParsedFile:
    """Parse one TypeScript / JavaScript file: module-scoped symbols, import
    specifiers (unresolved — see `resolve_imports`), extends / implements."""
    lang = lang_for_ts_path(path) or LANG_TS
    return parse_typescript_text(path, source, lang, _grammar_for(path))


def _iife_body(expr: Any) -> Any | None:
    """The statement block of an immediately invoked function expression —
    `(function () {})()`, `(() => {})()`, `!function () {}()`, `await (async
    () => {})()` — or None when the expression is anything else."""
    node = expr
    while node is not None and node.type in {
        "await_expression",
        "unary_expression",
        "parenthesized_expression",
    }:
        node = next((c for c in node.named_children), None)
    if node is None or node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    while fn is not None and fn.type == "parenthesized_expression":
        fn = next((c for c in fn.named_children), None)
    if fn is None or not _is_function_like(fn):
        return None
    body = fn.child_by_field_name("body")
    return body if body is not None and body.type == "statement_block" else None


_EXPORT_CLAUSE_RE = re.compile(r"\bexport\s*\{([^{}]*)\}(?!\s*from\b)")
_EXPORT_DEFAULT_NAME_RE = re.compile(
    r"\bexport\s+default\s+([A-Za-z_$][\w$]*)[^\S\n]*+;?[^\S\n]*+$", re.M
)


def export_clause_names(text: str) -> set[str]:
    """The LOCAL names a file exports through a clause that is not a re-export — `export
    { Collapsible, Trigger as CollapsibleTrigger }` (the name before `as`), `export
    default cfg` — so `const Collapsible = Primitive.Root` declared above it is an
    exported binding and a unit, exactly as `export const` would make it. The React
    component-kit idiom (shadcn/ui) writes every wrapper this way."""
    out: set[str] = set()
    for m in _EXPORT_CLAUSE_RE.finditer(text):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part or part.startswith("type "):
                continue
            local = part.split(" as ")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", local):
                out.add(local)
    out.update(m.group(1) for m in _EXPORT_DEFAULT_NAME_RE.finditer(text))
    return out


class _Span:
    """A node-like span (start_point / end_point) covering a member and the
    decorators written above it — tree-sitter keeps a class member's
    decorators as preceding siblings in the class body, so the member's own
    node starts after them."""

    def __init__(self, start: Any, end: Any) -> None:
        self.start_point = start.start_point
        self.end_point = end.end_point
        self.start_byte = start.start_byte
        self.end_byte = end.end_byte
        self.type = "decorated_member"
        self.prev_named_sibling = start.prev_named_sibling
        self.parent = end.parent


def _with_decorators(member: Any) -> Any:
    """The span of a class member including the decorators above it, so a call inside a
    decorator argument (`@ApiOkResponse({ type: Paginated(User) })`,
    `@UseGuards(AuthGuard('jwt'))`) attributes to the member it decorates and the
    member's chunk carries its route or guard."""
    first = member
    prev = member.prev_named_sibling
    while prev is not None and prev.type == "decorator":
        first = prev
        prev = prev.prev_named_sibling
    return member if first is member else _Span(first, member)


def _fold_overloads(symbols: list[Symbol]) -> list[Symbol]:
    """An overload signature (`function f(x: string): string;`, no body) beside its
    implementation is the same function — one unit, the implementation's row, so `f()`
    is not ambiguous. A bodiless declaration with no implementation (an
    interface method, an abstract method, a `declare function`) keeps its row."""
    implemented = {s.qualified_name for s in symbols if s.kind == "function" and s.has_body}
    return [
        s
        for s in symbols
        if not (s.kind == "function" and not s.has_body and s.qualified_name in implemented)
    ]


def parse_typescript_text(path: str, source: bytes, lang: str, grammar: str) -> ParsedFile:
    """`parse_typescript` with the language tag and grammar chosen by the caller (a Vue
    single-file component parses its script block, laid line-preservingly into a blank
    text, with the grammar its `lang` attribute names, whatever the file's extension)."""
    tree = _parser(grammar).parse(source)
    pf = ParsedFile(path=path, lang=lang, tree=tree, source=source)
    w = _Walker(pf=pf, source=source, mod=module_scope(path))
    w.exported = export_clause_names(source.decode("utf-8", "replace"))
    w.walk_program(tree.root_node)
    pf.symbols = _fold_overloads(pf.symbols)
    if b"@Component" in source:
        from server.code.domain.extract.lang_angular import stamp_selectors

        pf.symbols = stamp_selectors(pf.symbols, source)
    text = source.decode("utf-8", "replace")
    specs = [*w.imports, *_REQUIRE_RE.findall(text), *_DYNAMIC_IMPORT_RE.findall(text)]
    pf.includes = list(dict.fromkeys(s for s in specs if s and not s.startswith(("http", "node:"))))
    return pf


_MEMBER_KEY_TYPES: frozenset[str] = frozenset({"property_identifier", "identifier", "string"})
_LITERAL_TYPES: frozenset[str] = frozenset({"string", "number", "template_string", "true", "false"})


def _is_literal(node: Any) -> bool:
    node = _unwrap(node)
    if node is None:
        return False
    if node.type == "unary_expression":
        return any(c.type == "number" for c in node.named_children)
    return node.type in _LITERAL_TYPES


__all__ = [
    "IMPORT_EXTS",
    "KIND_CONST",
    "KIND_ENUM",
    "KIND_INTERFACE",
    "KIND_MODULE",
    "KIND_TYPE",
    "KIND_SUITE",
    "LANG_JS",
    "LANG_TS",
    "lang_for_ts_path",
    "parse_typescript",
    "parse_typescript_text",
    "export_clause_names",
]
