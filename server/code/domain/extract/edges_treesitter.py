"""Name-resolved reference edges for the fast tier (tier='treesitter').

Extracts call sites, include edges, and inheritance edges from a parsed
file. Call resolution is NAME-BASED and confidence-graded — the compiler
tier (scip) supersedes it wherever both exist:

    1.0  same-file unique definition
    0.8  method of the enclosing class (this->f() / unqualified f() in a
         member function whose class defines f)
    0.6  unique match across the repo-wide symbol name table
    (unresolved calls become no edge — a wrong edge is worse than a missing
    one; the scip tier fills the gap exactly — but they are RECORDED as
    CallMiss rows, keyed by bare callee name, so code.callers can say how
    many sites its count leaves out instead of a bare floor flag)

The repo-wide name table is supplied by the caller (code_repo lookup) as
{name: [(symbol_id, qualified_name)]} so this module stays CH-free and
unit-testable.

A constructor call is a call. C++ names the constructor only
in the temporary form `T(...)`; every other construction names the TYPE —
`T x(...)`, `T x{...}`, `new T(...)`, `T{...}`, and the factories
`std::make_shared<T>(...)` / `std::make_unique<T>(...)`, where the call
itself happens inside the library template's body. The walker below turns
each of those sites into a call to `T::T`, bound by the same ladder as any
other call, with the same-file rung narrowed by arity (the local overloads
carry their parameter lists) — a class with several constructors and no
arity match is declined as `ambiguous`, like any other multi-definition
name. The compiler tier (scip) emits the direct forms itself and the
converter adds the factory forms; both tiers agree on the target
qualified name `T::T`.

The remainder: a member-init list (`Holder() : widget_(3)`)
and an emplace call (`std::vector<Widget> v; v.emplace_back(9)`) construct
a type that is NOT in the caller's text but in a declaration the same file
holds — the field's declared type, the container's element type. Both
resolve through those declarations and then through the same ladder; a
type the file does not declare is declined, never guessed. A sole argument
that copies a value of the constructed type (`: header(header_)`) is not a
call to any indexed constructor and is declined too.

The header: ClickHouse keeps the class body in `Foo.h` and
the constructor in `Foo.cpp`, so 94% of member initializers name a field
the source file does not declare. The ingest resolves the class's header —
the sibling `<stem>.h`, then the file's `#include` list — reads its
declarations from the header's indexed chunks and hands them in as
`HeaderDecls`; the walker consults them only for a name this file does not
declare (a field, a base, a `using` alias for a container such as
`Chunks = std::vector<Chunk>`), and then binds through the same ladder. A
header the index does not hold leaves the site declined.
"""

from __future__ import annotations

import bisect
import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from server.code.domain.extract.text_lines import last_span_at
from server.code.domain.extract.treesitter import (
    ParsedFile,
    Symbol,
    _declarator_name,
    _function_declarator,
    file_symbol_id,
    file_unit,
    node_text,
)

# Call names that are language noise, never useful edges.
_NOISE_CPP = {
    "assert", "sizeof", "move", "forward", "get", "size", "begin", "end",
    "push_back", "emplace_back", "empty", "data", "reset", "release",
    "first", "second", "c_str", "str", "count", "find", "insert", "erase",
    "clear", "at", "back", "front", "make_shared", "make_unique",
}
_PY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The receiver spelling of a Python member call on an expression
PY_EXPR_RECEIVER = "()"
# A C++ member call on an expression the tree cannot name
CPP_EXPR_RECEIVER = "(expr)"

_NOISE_PYTHON = {
    "print", "len", "str", "int", "float", "bool", "list", "dict", "set",
    "tuple", "range", "enumerate", "zip", "isinstance", "super", "getattr",
    "setattr", "hasattr", "format", "join", "split", "strip", "append",
    "get", "items", "keys", "values", "open", "repr", "type", "sorted",
    "min", "max", "sum", "any", "all", "next", "iter", "id", "hash",
}
_NOISE_BASH = {
    "echo", "printf", "cd", "rm", "cp", "mv", "cat", "grep", "sed", "awk",
    "set", "export", "local", "return", "exit", "sleep", "mkdir", "touch",
    "test", "true", "false", "read", "shift", "trap", "wait", "kill",
    "chmod", "chown", "ln", "ls", "head", "tail", "sort", "uniq", "wc",
    "curl", "wget", "tar", "find", "xargs", "date", "dirname", "basename",
}
# The runtime, the standard library's methods, the test
# runner's and React's builtins, and the Zod / tRPC chain verbs — none of
# them is a definition of the repository
_NOISE_TS = {
    "log", "warn", "error", "info", "debug", "map", "filter", "reduce", "forEach", "find",
    "findIndex", "some", "every", "push", "pop", "shift", "unshift", "slice", "splice",
    "concat", "join", "split", "trim", "includes", "indexOf", "startsWith", "endsWith",
    "replace", "replaceAll", "toString", "toLowerCase", "toUpperCase", "toISOString",
    "toFixed", "toJSON", "then", "catch", "finally", "resolve", "reject", "all", "allSettled",
    "keys", "values", "entries", "assign", "freeze", "fromEntries", "parse", "stringify",
    "from", "of", "get", "set", "has", "delete", "add", "clear", "size", "test", "match",
    "exec", "require", "Number", "String", "Boolean", "Array", "Object", "Promise", "Date",
    "Error", "Map", "Set", "WeakMap", "Symbol", "BigInt", "RegExp", "parseInt", "parseFloat",
    "isNaN", "isArray", "isFinite", "setTimeout", "clearTimeout", "setInterval",
    "clearInterval", "queueMicrotask", "useState", "useEffect", "useMemo", "useCallback",
    "useRef", "useContext", "useReducer", "useLayoutEffect", "describe", "it", "expect",
    "beforeEach", "afterEach", "beforeAll", "afterAll", "vi", "jest", "mock", "fn", "spyOn",
    "object", "string", "number", "boolean", "array", "optional", "nullable", "nullish",
    "enum", "min", "max", "default", "infer", "input", "output", "query", "mutation",
    "subscription", "meta", "use", "transform", "refine", "pick", "omit", "extend", "merge",
    "partial", "strict", "passthrough", "literal", "union", "record", "tuple", "date",
    "bigint", "any", "unknown", "void", "never", "lazy", "coerce", "safeParse", "parseAsync",
    "sort", "reverse", "flat", "flatMap", "fill", "at", "padStart", "padEnd", "charAt",
    "substring", "substr", "localeCompare", "getTime", "now", "floor", "ceil", "round",
    "abs", "random", "sqrt", "pow", "trunc", "sign", "bind", "call", "apply", "emit", "on",
    "off", "once", "next", "return", "throw", "done", "await", "json", "text", "status",
    "send", "end", "write", "close", "open", "createElement", "cloneElement", "memo",
    "forwardRef", "Fragment", "Suspense", "toBe", "toEqual", "toHaveBeenCalled",
    "toHaveBeenCalledWith", "toBeDefined", "toBeUndefined", "toBeNull", "toBeTruthy",
    "toBeFalsy", "toContain", "toMatch", "toThrow", "toMatchObject", "toStrictEqual",
    "toHaveLength", "mockResolvedValue", "mockReturnValue", "mockImplementation",
}
# The std methods and constructors, the Option / Result
# combinators, the collection verbs; a `Vec::new()` binds to nobody's `new`
_NOISE_RUST = {
    "unwrap", "expect", "clone", "into", "from", "to_string", "to_owned", "as_ref", "as_mut",
    "as_str", "as_bytes", "len", "is_empty", "iter", "iter_mut", "into_iter", "map", "filter",
    "collect", "push", "pop", "get", "get_mut", "insert", "remove", "contains", "contains_key",
    "unwrap_or", "unwrap_or_default", "unwrap_or_else", "ok", "err", "ok_or", "ok_or_else",
    "map_err", "and_then", "or_else", "is_some", "is_none", "is_ok", "is_err", "Some", "Ok",
    "Err", "Box", "Vec", "String", "default", "with_capacity", "extend", "join", "split",
    "trim", "starts_with", "ends_with", "find", "replace", "parse", "chars", "bytes", "lines",
    "cloned", "copied", "enumerate", "zip", "rev", "take", "skip", "sum", "count", "any",
    "all", "min", "max", "fold", "next", "peek", "drain", "retain", "sort", "sort_by",
    "dedup", "first", "last", "keys", "values", "entry", "or_insert", "or_insert_with",
    "lock", "read", "write", "borrow", "borrow_mut", "deref", "drop", "send", "recv",
    "await", "spawn", "as_slice", "to_vec", "eq", "ne", "cmp", "partial_cmp", "hash", "fmt",
    "write_str", "write_fmt", "format", "flush", "read_to_string", "read_to_end", "clamp",
    "saturating_sub", "saturating_add", "wrapping_add", "checked_add", "checked_sub", "abs",
    "pow", "as_secs", "as_millis", "elapsed", "duration_since", "from_secs", "from_millis",
}
# scopes a Rust call may be qualified with that name no repository symbol
_NOISE_RUST_SCOPES = {
    "std", "core", "alloc", "Vec", "String", "Box", "Rc", "Arc", "Option", "Result", "Some",
    "Ok", "Err", "HashMap", "HashSet", "BTreeMap", "BTreeSet", "VecDeque", "Mutex", "RwLock",
    "Duration", "Instant", "SystemTime", "Path", "PathBuf", "Cow", "Cell", "RefCell", "Pin",
    "tokio", "serde", "serde_json", "tracing", "anyhow", "thiserror", "futures", "chrono",
    "reqwest", "axum", "hyper", "http", "bytes", "regex", "log", "env", "fs", "io", "mem",
    "ptr", "sync", "thread", "time", "fmt", "str", "char", "u8", "u16", "u32", "u64", "usize",
    "i8", "i16", "i32", "i64", "isize", "f32", "f64", "bool", "napi", "opentelemetry",
}
# Go's builtins and standard packages, Java's runtime types
_NOISE_GO: frozenset[str] = frozenset({
    "len", "cap", "make", "new", "append", "copy", "delete", "panic", "recover", "print",
    "println", "close", "string", "int", "int64", "int32", "uint64", "float64", "bool", "byte",
    "error", "fmt", "errors", "strings", "strconv", "time", "context", "sync", "os", "io",
    "bytes", "sort", "math", "log", "http", "json", "sql", "testing", "reflect", "regexp",
    "Sprintf", "Printf", "Println", "Errorf", "Fprintf", "Sprint", "New", "Is", "As", "Wrap",
    "Background", "TODO", "WithTimeout", "WithCancel", "Now", "Since", "Duration", "Marshal",
    "Unmarshal", "Itoa", "Atoi", "Join", "Split", "Contains", "TrimSpace", "HasPrefix",
    "ToLower", "ToUpper", "Fatal", "Fatalf", "Error", "Run", "Helper", "Parallel",
    "Skip", "Skipf", "Cleanup", "Lock", "Unlock", "Done", "Add", "Wait", "Close", "Err",
    "Scan", "Next",
})
_NOISE_JAVA: frozenset[str] = frozenset({
    "System", "String", "Integer", "Long", "Double", "Boolean", "Object", "Math", "List",
    "Map", "Set", "ArrayList", "HashMap", "HashSet", "Optional", "Stream", "Collectors",
    "Arrays", "Collections", "Objects", "StringBuilder", "Exception", "RuntimeException",
    "Thread", "Runnable", "println", "print", "printf", "format", "valueOf", "toString",
    "equals", "hashCode", "length", "size", "get", "put", "add", "remove", "isEmpty",
    "contains", "stream", "map", "filter", "collect", "forEach", "of", "toList", "join",
    "trim", "isBlank", "parseInt", "parseLong", "getClass", "getName", "assertEquals",
    "assertTrue", "assertFalse", "assertNotNull", "assertThat", "isEqualTo", "when",
    "thenReturn", "verify", "mock", "requireNonNull", "getLogger", "info", "debug", "warn",
    "error", "close", "next", "getString", "getInt", "getLong", "setString", "setInt",
    "setLong", "setObject",
})
def _import_keys(pf: ParsedFile) -> frozenset[str]:
    """The local names a TypeScript / JavaScript file imports.
    A noise-named call (`parse`, `get`) whose bare name or receiver is one of
    them is a call about the repository's own `parse` — it stays a site and
    climbs the import rung instead of being dropped as a builtin."""
    if pf.tree is None:
        return frozenset()
    if pf.lang == "python":
        # Every imported name may be the repository's; the
        # rung itself tells a library's (no present path) from the repo's
        from server.code.domain.extract.lang_python import py_import_bindings

        return frozenset(py_import_bindings(pf))
    if pf.lang not in ("typescript", "javascript"):
        return frozenset()
    from server.code.domain.extract.lang_typescript_imports import import_bindings

    return frozenset(k for k, v in import_bindings(pf).items() if _repo_shaped(v))


def _repo_shaped(spec: str) -> bool:
    """Can the specifier name an indexed module? Relative or absolute paths,
    the `@/` `~/` `#` aliases, and a pathed or scoped package
    (`@langfuse/shared`, `pkg/sub`) can; a bare package (`zod`, `react`)
    never does, so its chain verbs (`z.object()`) stay noise."""
    return spec.startswith((".", "/", "@/", "~/", "#")) or "/" in spec


def _imported_noise(
    resolver: Any, receiver: str, bare: str, name_table: dict[str, list[tuple[str, str, str]]],
) -> bool:
    """A noise-named call is still a site when the file
    imports its binding key from an indexed module — the repository's own
    `parse`, not the runtime's."""
    if getattr(resolver, "python", False):
        key = bare if receiver == "" else receiver.split(".", 1)[0]
        return bool(
            key in resolver.py_bindings
            and isinstance(resolver.import_bound(receiver, bare, name_table.get(bare, [])), tuple)
        )
    keys = frozenset(k for k, v in resolver.bindings.items() if _repo_shaped(v))
    return bool(
        resolver.script and _import_exempt(receiver, bare, keys)
        and isinstance(resolver.import_bound(receiver, bare, name_table.get(bare, [])), tuple)
    )


def _import_exempt(receiver: str, bare: str, keys: frozenset[str]) -> bool:
    """The call's binding key — the receiver of `p.parse()`, the bare name of
    `parse()` — is a name this file imports."""
    if not keys:
        return False
    key = bare if receiver == "" else receiver if "." not in receiver else ""
    return bool(key) and key in keys


_NOISE_BY_LANG = {
    "cpp": _NOISE_CPP, "python": _NOISE_PYTHON, "shell": _NOISE_BASH,
    "typescript": _NOISE_TS, "javascript": _NOISE_TS, "rust": _NOISE_RUST,
    "go": _NOISE_GO, "java": _NOISE_JAVA,
}

# Which AST node type carries a call, per language. One language per file,
# so the walkers key on the parsed file's own set.
_CALL_NODE_BY_LANG = {
    "cpp": "call_expression", "python": "call", "shell": "command",
    "typescript": "call_expression", "javascript": "call_expression", "rust": "call_expression",
    "go": "call_expression", "java": "method_invocation",
}
# `new Foo(…)` is a call site named `Foo` — the class is
# the unique definition of that bare name when it has no explicit
# constructor, its `constructor` method otherwise (bound by the same ladder)
_EXTRA_CALL_NODES_BY_LANG: dict[str, frozenset[str]] = {
    "typescript": frozenset({"new_expression"}), "javascript": frozenset({"new_expression"}),
}
_RUST_PATH_PREFIXES = ("crate::", "self::", "super::")


def _is_call(node: Any, call_node: str | None, lang: str) -> bool:
    if node.type == call_node or node.type in _EXTRA_CALL_NODES_BY_LANG.get(lang, frozenset()):
        return True
    return lang == "go" and _go_generic_conversion(node)


def _go_generic_conversion(node: Any) -> bool:
    """`proto.Null[int32](nil)`: tree-sitter reads `F[T](v)` with a type-like
    argument as a conversion to the generic type `F[T]`; when `F` is a
    function of the repository it is a call — the extractor
    checks the name table before binding it."""
    if node.type != "type_conversion_expression":
        return False
    t = node.child_by_field_name("type")
    return t is not None and t.type == "generic_type"


def _go_generic_conversion_name(node: Any, source: bytes) -> tuple[str, str]:
    """(package alias, bare name) of the generic type a conversion names."""
    t = node.child_by_field_name("type")
    inner = t.child_by_field_name("type") if t is not None else None
    if inner is None:
        return "", ""
    if inner.type == "qualified_type":
        pkg, name = inner.child_by_field_name("package"), inner.child_by_field_name("name")
        alias = node_text(pkg, source) if pkg is not None else ""
        return alias, (node_text(name, source) if name is not None else "")
    return "", node_text(inner, source) if inner.type == "type_identifier" else ""

# Factory templates whose first template argument is the type they construct.
# The constructor call itself is in the library's template body, so neither the
# parser nor the compiler sees it in the caller's TU; the type argument is the
# evidence.
_CTOR_FACTORIES = {
    "make_shared", "make_unique", "make_shared_for_overwrite",
    "make_unique_for_overwrite", "allocate_shared", "allocate_unique",
    "make_intrusive", "construct_at",
}
# C++ node types that construct a value by naming its type (not a call node).
_CTOR_NODE_TYPES = {"declaration", "new_expression", "compound_literal_expression"}
_TYPE_NAME_NODES = {"type_identifier", "qualified_identifier", "template_type"}


def is_constructor_name(qualified_name: str) -> bool:
    """`DB::Widget::Widget` — the last two `::` segments coincide. A trailing
    parameter list is ignored so a scip `name(sig)` rendering qualifies too.
    Destructors (`~Widget`) and ordinary methods do not."""
    head = qualified_name.split("(", 1)[0].strip()
    parts = head.split("::")
    return len(parts) >= 2 and bool(parts[-1]) and parts[-1] == parts[-2]


def _type_name(node: Any, source: bytes) -> str:
    """The class name a type node spells, template arguments dropped:
    `Widget`, `DB::Widget`, `Wrap<Widget>` -> `Wrap`, `std::vector<T>` ->
    `std::vector`. Empty for primitives, `auto` and anything else."""
    if node is None or node.type not in _TYPE_NAME_NODES:
        return ""
    if node.type == "template_type":
        name = node.child_by_field_name("name")
        return node_text(name, source) if name is not None else ""
    return node_text(node, source).split("<", 1)[0].strip()


def _first_template_type(template_args: Any, source: bytes) -> str:
    for c in template_args.named_children:
        if c.type == "type_descriptor":
            return _type_name(c.child_by_field_name("type"), source)
        if c.type in _TYPE_NAME_NODES:
            return _type_name(c, source)
        return ""
    return ""


def _argc(node: Any) -> int:
    """Arguments of an `argument_list` / `initializer_list` (comments skipped);
    0 when the node is absent (`new T`)."""
    if node is None:
        return 0
    return sum(1 for c in node.named_children if c.type != "comment")


def _ctor_of_call(call: Any, source: bytes) -> tuple[str, int] | None:
    """`std::make_shared<T>(args)` -> ("T", argc); None for any other call."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "qualified_identifier":
        fn = fn.child_by_field_name("name")
        if fn is None:
            return None
    if fn.type != "template_function":
        return None
    name = fn.child_by_field_name("name")
    if name is None or node_text(name, source) not in _CTOR_FACTORIES:
        return None
    targs = fn.child_by_field_name("arguments")
    if targs is None:
        return None
    t = _first_template_type(targs, source)
    if not t:
        return None
    return t, _argc(call.child_by_field_name("arguments"))


def _ctor_sites_of_node(node: Any, source: bytes) -> list[tuple[int, str, int]]:
    """(1-indexed line, type name as written, argc) for the constructions a
    `declaration` / `new_expression` / `compound_literal_expression` node
    performs. A declaration without an initializer list (`Holder h;`) is a
    default construction with argc 0; `T x = expr;` is not a site here — the
    expression on the right is its own node."""
    out: list[tuple[int, str, int]] = []
    if node.type == "declaration":
        t = _type_name(node.child_by_field_name("type"), source)
        if not t:
            return out
        for d in node.named_children:
            if d.type == "init_declarator":
                value = d.child_by_field_name("value")
                if value is not None and value.type in {"argument_list", "initializer_list"}:
                    out.append((d.start_point[0] + 1, t, _argc(value)))
            elif d.type == "identifier":
                out.append((d.start_point[0] + 1, t, 0))
        return out
    if node.type == "new_expression":
        t = _type_name(node.child_by_field_name("type"), source)
        if t:
            out.append((node.start_point[0] + 1, t, _argc(node.child_by_field_name("arguments"))))
        return out
    if node.type == "compound_literal_expression":
        t = _type_name(node.child_by_field_name("type"), source)
        if t:
            out.append((node.start_point[0] + 1, t, _argc(node.child_by_field_name("value"))))
    return out


def _arity_ok(signature: str, argc: int) -> bool:
    """Does a parser-tier parameter list `(a, b = 1, ...)` admit `argc`
    arguments? Defaults make a parameter optional, a pack admits any count.
    A signature that is not a parameter list (the scip hash) admits all."""
    sig = signature.strip()
    if not sig.startswith("(") or not sig.endswith(")") or sig.startswith("(scip:"):
        return True
    inner = sig[1:-1].strip()
    if not inner or inner == "void":
        return argc == 0
    params: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(inner):
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        elif ch == "," and depth == 0:
            params.append(inner[start:i])
            start = i + 1
    params.append(inner[start:])
    if any("..." in p for p in params):
        return argc >= sum(1 for p in params if "..." not in p and "=" not in p)
    required = sum(1 for p in params if "=" not in p)
    return required <= argc <= len(params)


def ctor_sites(pf: ParsedFile) -> list[tuple[int, str, int]]:
    """Every constructor site of a C++ parse: (line, type name as written,
    argc), factory calls included. Empty for other languages."""
    out: list[tuple[int, str, int]] = []
    if pf.tree is None or pf.lang != "cpp":
        return out
    source = pf.source

    def walk(node: Any) -> None:
        for child in node.named_children:
            if child.type == "call_expression":
                hit = _ctor_of_call(child, source)
                if hit is not None:
                    out.append((child.start_point[0] + 1, hit[0], hit[1]))
            elif child.type in _CTOR_NODE_TYPES:
                out.extend(_ctor_sites_of_node(child, source))
            walk(child)

    walk(pf.tree.root_node)
    return out


# ---- the remainder: member-init lists and emplace calls ----------

_EMPLACE_METHODS = {"emplace_back", "emplace_front", "emplace", "try_emplace", "emplace_hint"}
# container (template arguments dropped, qualifier dropped) -> the index of
# the ELEMENT among its template arguments
_ELEMENT_ARG = {
    "vector": 0, "deque": 0, "list": 0, "forward_list": 0,
    "PODArray": 0, "PaddedPODArray": 0,
    "set": 0, "multiset": 0, "unordered_set": 0, "unordered_multiset": 0,
    "flat_hash_set": 0, "node_hash_set": 0,
    "stack": 0, "queue": 0, "priority_queue": 0, "optional": 0,
    "map": 1, "multimap": 1, "unordered_map": 1, "unordered_multimap": 1,
    "flat_hash_map": 1, "node_hash_map": 1,
}
_SEQUENCE = {"vector", "deque", "list", "forward_list", "PODArray", "PaddedPODArray"}
_SETS = {"set", "multiset", "unordered_set", "unordered_multiset", "flat_hash_set", "node_hash_set"}
_MAPS = {c for c, i in _ELEMENT_ARG.items() if i == 1}
_ADAPTORS = {"stack", "queue", "priority_queue", "optional"}
_MOVE_WRAPPERS = {"std::move", "move", "std::forward", "forward"}
_LITERALS = {
    "number_literal", "string_literal", "char_literal", "true", "false", "null",
    "nullptr", "concatenated_string", "raw_string_literal", "user_defined_literal",
}


@dataclass(frozen=True)
class CtorSite:
    """A construction whose type comes from a declaration, not the call text.
    `rule`: member_init | base_init | emplace. `written` is the type as the
    declaration spells it ("" when it could not be determined). `decline`:
    "" (bind by the ladder), `unresolved` (neither the file nor a resolved
    header declares the type or the sole argument's type), `copy` (the sole
    argument is a value of the constructed type: a copy or move, never an
    indexed constructor)."""

    line: int
    written: str
    argc: int
    rule: str
    decline: str = ""
    detail: str = ""  # the initializer / container as written, for the bench
    # Where the type came from when not from this file's own
    # declarations — "header" (the class body in the resolved header),
    # "alias" (a `using` the file or a header declares), "header+alias"
    via: str = ""
    # what a header could still supply for a declined site: "field" (a name
    # nothing here declares), "alias" (a container type that is no known
    # template) — the ingest reads headers only while such sites remain
    need: str = ""


def _skipped_args(method: str, container: str, explicit_type: bool) -> int | None:
    """How many leading arguments of `container.method(...)` are NOT forwarded
    to the element's constructor (a position or hint iterator, a map key);
    None when the call constructs no element of the container's element
    type (`map.emplace(k, v)` builds a pair, the value is copied in)."""
    if explicit_type:  # `x.emplace<T>(args)`: T is in the text, all args forwarded
        return 0
    if method in {"emplace_back", "emplace_front"}:
        return 0 if container in _SEQUENCE else None
    if method == "emplace":
        if container in _SEQUENCE:
            return 1
        if container in _SETS or container in _ADAPTORS:
            return 0
        return None
    if method == "try_emplace":
        return 1 if container in _MAPS else None
    if method == "emplace_hint":
        return 1 if container in _SETS else None
    return None


def _template_parts(type_node: Any, source: bytes) -> tuple[str, list[str]]:
    """`std::vector<Widget>` -> ("vector", ["Widget"]); a type argument that
    is a pointer or reference (`Widget *`) is "" — no value is constructed."""
    node = type_node
    while node is not None and node.type == "qualified_identifier":
        node = node.child_by_field_name("name")
    if node is None or node.type != "template_type":
        return (_type_name(type_node, source).split("::")[-1], [])
    name = node.child_by_field_name("name")
    args = node.child_by_field_name("arguments")
    out: list[str] = []
    for c in (args.named_children if args is not None else []):
        if c.type == "type_descriptor":
            if c.child_by_field_name("declarator") is not None:
                out.append("")
            else:
                out.append(_type_name(c.child_by_field_name("type"), source))
        elif c.type in _TYPE_NAME_NODES:
            out.append(_type_name(c, source))
        else:
            out.append("")
    return (node_text(name, source) if name is not None else "", out)


_TYPE_PREFIXES = ("const ", "volatile ", "typename ", "struct ", "class ")
# what the grammar parses as `primitive_type` (no constructor to bind), for
# the text-level reading of an alias
_PRIMITIVES = {
    "bool", "char", "short", "int", "long", "float", "double", "void", "signed", "unsigned",
    "wchar_t", "char8_t", "char16_t", "char32_t", "size_t", "ssize_t", "intptr_t", "uintptr_t",
    "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
}


def _strip_type_prefixes(text: str) -> str:
    t = text.strip()
    changed = True
    while changed:
        changed = False
        for q in _TYPE_PREFIXES:
            if t.startswith(q):
                t = t[len(q):].lstrip()
                changed = True
    return t.removeprefix("::").strip()


def _type_parts_of_text(text: str) -> tuple[str, list[str]]:
    """The type an alias spells, at the text level (it comes from another
    parse): `std::vector<Chunk>` -> ("vector", ["Chunk"]), `Widget` ->
    ("Widget", []), `Chunk *` / `Chunk &` -> ("", []) — a pointer or a
    reference constructs nothing. A template argument that is a pointer, a
    reference or a value (`std::array<int, 4>`) is ""; a qualified argument
    keeps its qualification (`DB::Chunk`), template arguments of its own
    dropped. Anything trailing the template list (`::iterator`) is not a
    container: ("", [])."""
    t = _strip_type_prefixes(text)
    if not t or t.endswith(("*", "&")):
        return "", []
    lt = t.find("<")
    if lt < 0:
        return t.split("::")[-1], []
    depth = 0
    end = -1
    for i in range(lt, len(t)):
        if t[i] == "<":
            depth += 1
        elif t[i] == ">":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0 or t[end + 1 :].strip():
        return "", []
    head = t[:lt].strip().split("::")[-1]
    inner = t[lt + 1 : end]
    args: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(inner):
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(inner[start:i])
            start = i + 1
    args.append(inner[start:])
    out: list[str] = []
    for a in args:
        a = _strip_type_prefixes(a)
        if not a or a.endswith(("*", "&")) or not (a[0].isalpha() or a[0] == "_"):
            out.append("")
        elif a.split()[0] in _PRIMITIVES:
            out.append("")
        else:
            out.append(a.split("<", 1)[0].strip())
    return head, out


@dataclass(frozen=True)
class _Decl:
    """One declared name's type, read once from its type node so a
    declaration from another parse (a header's field) is the
    same thing as one from this file. `written`: the class name, template
    arguments dropped ("" for a primitive or `auto`); `container` / `targs`:
    `_template_parts`; `plain`: not a pointer or reference declarator;
    `text`: the type as spelled, for the declared-twice check."""

    written: str
    container: str
    targs: tuple[str, ...]
    plain: bool
    text: str


def _decl_of(type_node: Any, source: bytes, plain: bool) -> _Decl:
    container, targs = _template_parts(type_node, source)
    return _Decl(
        _type_name(type_node, source), container, tuple(targs), plain, node_text(type_node, source),
    )


class _Aliases:
    """Bare alias name -> the type text it names, from `using X = T;` and
    `typedef T X;` at any depth of a parse (a template alias `template
    <typename T> using Vec = …` is skipped: its argument is unknown at the
    use). A name aliased twice to different texts maps to None — declined,
    never guessed. `resolve_text` chases alias-of-alias up to three hops
    (`Chunks` -> `std::vector<Chunk>`; `ProcessorPtr` ->
    `std::shared_ptr<IProcessor>`)."""

    _HOPS = 3

    def __init__(self) -> None:
        self.table: dict[str, str | None] = {}

    def _put(self, name: str, text: str) -> None:
        name = name.strip()
        text = " ".join(text.split())
        if not name or not text:
            return
        if name not in self.table:
            self.table[name] = text
        elif self.table[name] != text:
            self.table[name] = None

    def add_tree(self, root: Any, source: bytes) -> None:
        def walk(node: Any) -> None:
            for child in node.named_children:
                if child.type == "alias_declaration":
                    if node.type != "template_declaration":
                        name = child.child_by_field_name("name")
                        typ = child.child_by_field_name("type")
                        if name is not None and typ is not None:
                            self._put(node_text(name, source), node_text(typ, source))
                    continue
                if child.type == "type_definition":
                    typ = child.child_by_field_name("type")
                    for d in child.children_by_field_name("declarator"):
                        if d.type == "type_identifier" and typ is not None:
                            self._put(node_text(d, source), node_text(typ, source))
                    continue
                walk(child)

        walk(root)

    def merge(self, other: _Aliases) -> None:
        """This table's entries win; the other's fill the gaps."""
        for name, text in other.table.items():
            if name not in self.table:
                self.table[name] = text

    def resolve_text(self, name: str) -> str | None:
        """The type text `name` finally names, or None when `name` is not an
        alias here (or is aliased twice). A plain alias-of-alias is
        followed; a template, a pointer and an unknown name end the chase."""
        cur = name.split("::")[-1].strip()
        seen: set[str] = set()
        text: str | None = None
        for _ in range(self._HOPS):
            if cur in seen or cur not in self.table:
                break
            seen.add(cur)
            got = self.table[cur]
            if got is None:
                return None
            text = got
            head, targs = _type_parts_of_text(got)
            if not head or targs or head not in self.table:
                break
            cur = head
        return text


def _declared_name(declarator: Any, source: bytes) -> tuple[str, bool] | None:
    """(name, is_plain_value) of a declarator: `w`, `& w`, `* w` (the latter
    two are not plain values); None for arrays, functions and anything else."""
    node = declarator
    plain = True
    while node is not None:
        if node.type in {"identifier", "field_identifier"}:
            return node_text(node, source), plain
        if node.type == "init_declarator":
            node = node.child_by_field_name("declarator")
            continue
        if node.type in {"pointer_declarator", "reference_declarator"}:
            plain = False
            node = node.child_by_field_name("declarator") or next(
                (c for c in node.named_children if c.type.endswith("declarator")
                 or c.type in {"identifier", "field_identifier"}), None,
            )
            continue
        return None
    return None


class _Scope:
    """Declared names visible to one function: its parameters, its local
    declarations and the fields of its class (the class body in this file,
    or in the header the ingest resolved for it).
    name -> _Decl; a name declared twice with different types maps to None
    (declined rather than guessed). `from_header` names the fields the
    header supplied, so a site can say where its type came from."""

    def __init__(self, source: bytes, aliases: _Aliases | None = None) -> None:
        self.source = source
        self.types: dict[str, _Decl | None] = {}
        self.params: dict[str, _Decl | None] = {}
        self.from_header: set[str] = set()
        self.aliases = aliases if aliases is not None else _Aliases()

    def _add(self, into: dict[str, _Decl | None], name: str, decl: _Decl) -> None:
        if name not in into:
            into[name] = decl
            return
        prev = into[name]
        if prev is not None and prev.text != decl.text:
            into[name] = None

    def add_declarations(self, node: Any, kinds: tuple[str, ...]) -> None:
        """Every `declaration` / `field_declaration` / `parameter_declaration`
        under `node`, not descending into nested classes."""
        for child in node.named_children:
            if child.type in {"class_specifier", "struct_specifier", "union_specifier"}:
                continue
            if child.type in kinds:
                type_node = child.child_by_field_name("type")
                if type_node is not None:
                    is_param = child.type != "field_declaration" and child.type != "declaration"
                    into = self.params if is_param else self.types
                    for d in child.named_children:
                        if d is type_node or d.type in {
                            "type_qualifier", "storage_class_specifier",
                        }:
                            continue
                        got = _declared_name(d, self.source)
                        if got is not None:
                            self._add(into, got[0], _decl_of(type_node, self.source, got[1]))
            self.add_declarations(child, kinds)

    def add_header_fields(self, fields: dict[str, _Decl | None]) -> None:
        """A header's class fields, behind whatever this file declares."""
        for name, decl in fields.items():
            if name not in self.types:
                self.types[name] = decl
                self.from_header.add(name)

    def lookup(self, name: str) -> _Decl | None:
        """The declaration; None when the name is not declared here or is
        declared twice with different types."""
        if name in self.types:
            return self.types[name]
        return self.params.get(name)

    def bare_type_of(self, decl: _Decl) -> str:
        """The bare class name a declaration's type finally names — through
        a plain alias (`using Port = OutputPort`) when the file or its
        headers declare one."""
        bare = decl.written.split("::")[-1]
        if bare and not decl.targs and bare not in _ELEMENT_ARG:
            text = self.aliases.resolve_text(bare)
            if text:
                head, targs = _type_parts_of_text(text)
                if head and not targs:
                    return head
        return bare


def _class_bodies(root: Any, source: bytes) -> dict[str, list[Any]]:
    """Bare class name -> class/struct specifier nodes with a body, whole file."""
    out: dict[str, list[Any]] = {}

    def walk(node: Any) -> None:
        for child in node.named_children:
            if child.type in {"class_specifier", "struct_specifier"}:
                name = child.child_by_field_name("name")
                if name is not None and child.child_by_field_name("body") is not None:
                    out.setdefault(node_text(name, source).split("::")[-1], []).append(child)
            walk(child)

    walk(root)
    return out


def _class_bodies_qualified(root: Any, source: bytes) -> list[tuple[str, Any]]:
    """(qualified name, specifier node) for every class/struct with a body,
    the qualification being the enclosing namespaces and classes as
    `parse_cpp` spells a symbol (`DB::Foo::Impl`)."""
    out: list[tuple[str, Any]] = []

    def walk(node: Any, scope: tuple[str, ...]) -> None:
        for child in node.named_children:
            if child.type == "namespace_definition":
                name = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                ns = node_text(name, source) if name is not None else ""
                if body is not None:
                    walk(body, scope + tuple(ns.split("::")) if ns else scope)
                continue
            if child.type in {"class_specifier", "struct_specifier"}:
                name = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if name is not None and body is not None:
                    qname = "::".join(scope + tuple(node_text(name, source).split("::")))
                    out.append((qname, child))
                    walk(body, tuple(qname.split("::")))
                continue
            walk(child, scope)

    walk(root, ())
    return out


@dataclass
class HeaderDecls:
    """What the headers a source file reaches declare: each
    class body's plain-value fields, each class's bases and the type
    aliases — read from the headers' indexed chunks by the ingest and
    handed to the walker, which consults them only for a name this file
    does not declare. `class_for` answers for one class: the header class
    whose qualified name the out-of-line member's owner spells (`Foo`,
    `Foo::Impl`; matched as a `::`-suffix, so `DB::Foo` answers `Foo`), and
    declines when several do."""

    paths: list[str] = field(default_factory=list)
    # bare class name -> [(qualified name, fields, constructor parameter lists as declared)]
    classes: dict[str, list[tuple[str, dict[str, _Decl | None], tuple[str, ...]]]] = field(
        default_factory=dict
    )
    # class qualified name -> base names as written (template arguments dropped)
    bases: dict[str, list[str]] = field(default_factory=dict)
    aliases: _Aliases = field(default_factory=_Aliases)

    def add_source(self, path: str, source: bytes) -> None:
        from server.code.domain.extract.treesitter import parse_cpp

        pf = parse_cpp(path, source)
        if pf.tree is None:
            return
        root = pf.tree.root_node
        for qname, node in _class_bodies_qualified(root, source):
            body = node.child_by_field_name("body")
            sc = _Scope(source)
            bare = qname.split("::")[-1]
            sigs: tuple[str, ...] = ()
            if body is not None:
                sc.add_declarations(body, ("field_declaration",))
                sigs = _ctor_signatures(body, bare, source)
            self.classes.setdefault(bare, []).append((qname, sc.types, sigs))
        for derived, base in pf.inherits:
            self.bases.setdefault(derived, []).append(base)
        self.aliases.add_tree(root, source)
        self.paths.append(path)

    def class_for(
        self, owner: str,
    ) -> tuple[str, dict[str, _Decl | None], tuple[str, ...]] | None:
        owner = owner.strip(": ")
        if not owner:
            return None
        cands = self.classes.get(owner.split("::")[-1], [])
        exact = [c for c in cands if c[0] == owner or c[0].endswith("::" + owner)]
        return exact[0] if len(exact) == 1 else None

    def bases_for(self, owner: str) -> list[str]:
        hit = self.class_for(owner)
        return list(self.bases.get(hit[0], [])) if hit is not None else []

    def declared_ctors(self, written: str) -> int:
        """How many constructors the header declares for the class `written`
        names (0 when no header read declares the class): the overload set
        the repo-wide rung cannot see. `= default` and `= delete`
        declarations count — a copy constructor is an overload too."""
        hit = self.class_for(written)
        return len(hit[2]) if hit is not None else 0

    _DEPTH = 3

    def fields_for(self, owner: str) -> dict[str, _Decl | None]:
        """The class's fields, then its bases' (the headers the ingest read
        hold them or not), nearest declaration first, three levels up: an
        `outputs.emplace_back(header)` in a processor constructs an
        `OutputPort` that `IProcessor` declares."""
        out: dict[str, _Decl | None] = {}
        todo = [owner]
        seen: set[str] = set()
        for _ in range(self._DEPTH):
            nxt: list[str] = []
            for o in todo:
                hit = self.class_for(o)
                if hit is None or hit[0] in seen:
                    continue
                seen.add(hit[0])
                for name, decl in hit[1].items():
                    out.setdefault(name, decl)
                nxt.extend(self.bases.get(hit[0], []))
            if not nxt:
                break
            todo = nxt
        return out


def _ctor_signatures(body: Any, cls_bare: str, source: bytes) -> tuple[str, ...]:
    """The parameter lists of the constructors a class body declares or
    defines (`Widget();`, `explicit Widget(int);`, `Widget(const Widget &)
    = default;`, a template constructor), nested classes excluded."""
    out: list[str] = []
    for child in body.named_children:
        node = child
        if node.type == "template_declaration":
            node = next((c for c in node.named_children if "declaration" in c.type
                         or c.type == "function_definition"), None)
        if node is None or node.type not in {
            "declaration", "function_definition", "field_declaration",
        }:
            continue
        fdecl = _function_declarator(node)
        if fdecl is None:
            continue
        if _declarator_name(fdecl, source).split("::")[-1] != cls_bare:
            continue
        params = fdecl.child_by_field_name("parameters")
        out.append(node_text(params, source) if params is not None else "()")
    return tuple(out)


def _enclosing_class_node(fn: Any) -> Any:
    node = fn.parent
    while node is not None:
        if node.type in {"class_specifier", "struct_specifier"}:
            return node
        if node.type in {"namespace_definition", "translation_unit", "function_definition"}:
            return None
        node = node.parent
    return None


def _owner_of(fn: Any, source: bytes) -> str:
    """The class an out-of-line member belongs to, as its declarator spells
    it: `Foo::Foo` / `Foo::add` -> `Foo`, `Foo::Impl::Impl` -> `Foo::Impl`;
    "" for an in-class definition (its class body encloses it)."""
    fdecl = _function_declarator(fn)
    name = _declarator_name(fdecl, source) if fdecl is not None else ""
    return name.rsplit("::", 1)[0] if "::" in name else ""


def _scope_of(
    fn: Any, source: bytes, classes: dict[str, list[Any]], cls_bare: str,
    header: HeaderDecls | None = None, owner: str = "", aliases: _Aliases | None = None,
) -> _Scope:
    """Parameters, locals and the class's fields visible inside `fn` — the
    class body from this file, or, when the file holds none, from the
    header the ingest resolved."""
    sc = _Scope(source, aliases)
    fdecl = _function_declarator(fn)
    params = fdecl.child_by_field_name("parameters") if fdecl is not None else None
    if params is not None:
        sc.add_declarations(params, ("parameter_declaration", "optional_parameter_declaration"))
    body = fn.child_by_field_name("body")
    if body is not None:
        sc.add_declarations(body, ("declaration",))
    cls = _enclosing_class_node(fn)
    bodies = [cls] if cls is not None else classes.get(cls_bare, [])
    for c in bodies:
        b = c.child_by_field_name("body")
        if b is not None:
            sc.add_declarations(b, ("field_declaration",))
    if not bodies and header is not None:
        sc.add_header_fields(header.fields_for(owner or cls_bare))
    return sc


def _sole_argument(args: Any, source: bytes, skip: int = 0) -> tuple[str, str]:
    """Of the arguments after the first `skip`: ("", "") for zero or several;
    ("literal", "") for one literal; ("name", n) for one identifier (also
    `std::move(n)`, `this->n`); ("expr", "") for any other single expression
    (a call, a member of something, a dereference) — its type is not in the
    file."""
    items = (
        [c for c in args.named_children if c.type != "comment"][skip:] if args is not None else []
    )
    if len(items) != 1:
        return "", ""
    c = items[0]
    if c.type in _LITERALS:
        return "literal", ""
    if c.type == "call_expression":
        fn = c.child_by_field_name("function")
        fn_text = node_text(fn, source).split("<", 1)[0] if fn is not None else ""
        inner = c.child_by_field_name("arguments")
        inner_items = (
            [x for x in inner.named_children if x.type != "comment"] if inner is not None else []
        )
        if fn_text in _MOVE_WRAPPERS and len(inner_items) == 1:
            c = inner_items[0]
        else:
            return "expr", ""
    if c.type == "identifier":
        return "name", node_text(c, source)
    if c.type == "field_expression":
        arg = c.child_by_field_name("argument")
        fld = c.child_by_field_name("field")
        if arg is not None and arg.type == "this" and fld is not None:
            return "name", node_text(fld, source)
    return "expr", ""


def _decline_for_args(
    args: Any, scope: _Scope, constructed: str, skip: int = 0,
) -> tuple[str, str]:
    """The guard on a sole forwarded argument: a value of the constructed
    type is a copy or a move (`copy`); a value whose type the file does not
    declare, or an expression, is `unresolved`. Literals, zero and several
    arguments pass. Second item: what a header could supply — "field" when
    the sole argument is a name nothing here declares."""
    kind, name = _sole_argument(args, scope.source, skip)
    if kind in {"", "literal"}:
        return "", ""
    if kind == "expr":
        return "unresolved", ""
    got = scope.lookup(name)
    if got is None:
        return "unresolved", "field"
    if scope.bare_type_of(got) == constructed.split("::")[-1]:
        return "copy", ""
    return "", ""


def _member_init_sites(
    fn: Any, source: bytes, classes: dict[str, list[Any]], bases: dict[str, list[str]],
    header: HeaderDecls | None = None, aliases: _Aliases | None = None,
) -> list[CtorSite]:
    """`Ctor(...) : member_(args), Base(args) {}` — one site per initializer
    whose member's declared type (a plain value field of the class, declared
    in this file or in the resolved header) or base class is named; a
    pointer/reference member, a field nothing declares and a member of a
    template/library type are declined (`unresolved`) — the last ones
    silently, since there is no constructor to ask about."""
    init_list = next(
        (c for c in fn.named_children if c.type == "field_initializer_list"), None,
    )
    if init_list is None:
        return []
    fdecl = _function_declarator(fn)
    if fdecl is None:
        return []
    cls_bare = _declarator_name(fdecl, source).split("::")[-1]
    owner = _owner_of(fn, source)
    scope = _scope_of(fn, source, classes, cls_bare, header, owner, aliases)
    class_bases = bases.get(cls_bare, [])
    bases_via = ""
    if not class_bases and header is not None and cls_bare not in classes:
        class_bases = header.bases_for(owner or cls_bare)
        bases_via = "header" if class_bases else ""
    out: list[CtorSite] = []
    for init in init_list.named_children:
        if init.type != "field_initializer":
            continue
        name_node = init.named_children[0] if init.named_children else None
        if name_node is None:
            continue
        name = node_text(name_node, source).split("<", 1)[0].strip()
        args = next(
            (c for c in init.named_children if c.type in {"argument_list", "initializer_list"}),
            None,
        )
        line = init.start_point[0] + 1
        argc = _argc(args)
        base = next(
            (b for b in class_bases if b == name or b.split("::")[-1] == name.split("::")[-1]),
            None,
        )
        if base is not None:
            decline, need = _decline_for_args(args, scope, base)
            out.append(CtorSite(line, base, argc, "base_init", decline, name, bases_via, need))
            continue
        got = scope.types.get(name)
        if got is None:
            out.append(CtorSite(line, "", argc, "member_init", "unresolved", name, "", "field"))
            continue
        written = got.written
        if not got.plain or not written:
            # a pointer/reference member, or a primitive/auto: no constructor
            continue
        via = "header" if name in scope.from_header else ""
        if not got.targs:
            # `Port port_;` with `using Port = OutputPort;`: the field's type is
            # the alias's target (a pointer alias constructs nothing)
            text = scope.aliases.resolve_text(written)
            if text:
                head, targs = _type_parts_of_text(text)
                if not head:
                    continue
                written = head
                via = via + "+alias" if via else "alias"
        decline, need = _decline_for_args(args, scope, written)
        out.append(CtorSite(line, written, argc, "member_init", decline, name, via, need))
    return out


def _emplace_site(
    call: Any, fn: Any, source: bytes, classes: dict[str, list[Any]],
    header: HeaderDecls | None = None, aliases: _Aliases | None = None,
) -> CtorSite | None:
    """`v.emplace_back(a, b)` -> a site constructing v's element type, from
    v's declaration in the function (a local, a parameter) or its class (a
    field, `this->v` too; the class body in this file or in the resolved
    header). A container spelled through an alias (`Chunks chunks;`,
    `using Chunks = std::vector<Chunk>`) is read through the alias when the
    file or a resolved header declares it. Declined `unresolved` when the
    container's type is not a known container template."""
    fnode = call.child_by_field_name("function")
    if fnode is None or fnode.type != "field_expression":
        return None
    field_node = fnode.child_by_field_name("field")
    if field_node is None:
        return None
    explicit = ""
    if field_node.type == "template_method":
        targs = field_node.child_by_field_name("arguments")
        explicit = _first_template_type(targs, source) if targs is not None else ""
        field_node = field_node.child_by_field_name("name")
        if field_node is None:
            return None
    method = node_text(field_node, source)
    if method not in _EMPLACE_METHODS:
        return None
    line = call.start_point[0] + 1
    args = call.child_by_field_name("arguments")
    argc = _argc(args)
    recv = fnode.child_by_field_name("argument")
    detail = node_text(fnode, source)[:200]
    if recv is not None and recv.type == "field_expression":
        inner_arg = recv.child_by_field_name("argument")
        inner_fld = recv.child_by_field_name("field")
        recv = inner_fld if inner_arg is not None and inner_arg.type == "this" else None
    if recv is None or recv.type not in {"identifier", "field_identifier"}:
        return CtorSite(line, "", argc, "emplace", "unresolved", detail)
    owner = _owner_of(fn, source)
    cls_bare = owner.split("::")[-1] if owner else ""
    scope = _scope_of(fn, source, classes, cls_bare, header, owner, aliases)
    recv_name = node_text(recv, source)
    got = scope.lookup(recv_name)
    if got is None:
        return CtorSite(line, "", argc, "emplace", "unresolved", detail, "", "field")
    via = "header" if recv_name in scope.from_header else ""
    element, skipped, via, need = _element_of(got, method, explicit, scope, via)
    if element is None:
        return None  # a pointer element or a pointer alias: nothing is constructed
    if not element or argc < skipped:
        return CtorSite(line, "", argc, "emplace", "unresolved", detail, via, need)
    decline, need = _decline_for_args(args, scope, element, skipped)
    return CtorSite(line, element, argc - skipped, "emplace", decline, detail, via, need)


def _with_alias(via: str) -> str:
    return via if via.endswith("alias") else (via + "+alias" if via else "alias")


def _element_of(
    got: _Decl, method: str, explicit: str, scope: _Scope, via: str,
) -> tuple[str | None, int, str, str]:
    """(element type, arguments not forwarded, via, need) of an emplace on a
    receiver declared as `got`: None when nothing is constructed (a pointer
    element, an alias for a pointer), "" when the container is unknown —
    with `need` "alias" when a `using` nobody read could still name it."""
    container, targs_names = got.container, list(got.targs)
    if not explicit and container not in _ELEMENT_ARG and not targs_names:
        text = scope.aliases.resolve_text(container)
        if text is None:
            return "", 0, via, "alias"
        container, targs_names = _type_parts_of_text(text)
        if not container:
            return None, 0, via, ""
        via = _with_alias(via)
    skipped = 0
    if explicit:
        element = explicit
    else:
        idx = _ELEMENT_ARG.get(container)
        skip = _skipped_args(method, container, False)
        if idx is None or skip is None or len(targs_names) <= idx:
            return "", 0, via, ""
        element, skipped = targs_names[idx], skip
    if not element:
        return None, skipped, via, ""
    # an element spelled through an alias: `using Item = Widget` names the
    # class; a pointer alias (`ProcessorPtr`) constructs nothing
    text = scope.aliases.resolve_text(element)
    if text:
        head, _targs = _type_parts_of_text(text)
        if not head:
            return None, skipped, via, ""
        element, via = head, _with_alias(via)
    return element, skipped, via, ""


def remainder_ctor_sites(pf: ParsedFile, header: HeaderDecls | None = None) -> list[CtorSite]:
    """Every member-init, base-init and emplace construction of a C++ parse,
    bound or declined. Empty for other languages. Pure over the parse plus, when
    the ingest resolved one, the header's declarations; the ladder runs in
    `extract_edges_and_misses`."""
    out: list[CtorSite] = []
    if pf.tree is None or pf.lang != "cpp":
        return out
    source = pf.source
    root = pf.tree.root_node
    classes = _class_bodies(root, source)
    bases: dict[str, list[str]] = {}
    for derived, base in pf.inherits:
        bases.setdefault(derived.split("::")[-1], []).append(base)
    aliases = _Aliases()
    aliases.add_tree(root, source)
    if header is not None:
        aliases.merge(header.aliases)

    def walk(node: Any, fn: Any) -> None:
        for child in node.named_children:
            if child.type == "function_definition":
                out.extend(_member_init_sites(child, source, classes, bases, header, aliases))
                walk(child, child)
                continue
            if child.type == "call_expression" and fn is not None:
                site = _emplace_site(child, fn, source, classes, header, aliases)
                if site is not None:
                    out.append(site)
            walk(child, fn)

    walk(root, None)
    return out


@dataclass(frozen=True)
class Edge:
    edge_kind: str  # calls | includes | inherits
    from_symbol_id: str
    to_symbol_id: str
    from_qualified_name: str
    to_qualified_name: str
    path: str
    line: int  # 1-indexed call-site line
    confidence: float


def _call_name(node: Any, source: bytes) -> str:
    """Callee name of a call node — bare, qualified, or member — across the
    three grammars (cpp call_expression, python call, bash command)."""
    if node.type == "command":  # bash: first word is the command name
        name = node.child_by_field_name("name")
        return node_text(name, source) if name is not None else ""
    if node.type == "new_expression":  # typescript: `new Foo()` / `new ns.Foo()`
        ctor = node.child_by_field_name("constructor")
        if ctor is None:
            return ""
        if ctor.type == "member_expression":
            prop = ctor.child_by_field_name("property")
            return node_text(prop, source) if prop is not None else ""
        return node_text(ctor, source) if ctor.type == "identifier" else ""
    if node.type == "method_invocation":  # java: obj.f(...) / f(...) — the name field
        name = node.child_by_field_name("name")
        return node_text(name, source) if name is not None else ""
    if node.type == "type_conversion_expression":  # `F[T](v)`
        return _go_generic_conversion_name(node, source)[1]
    fn = node.child_by_field_name("function")
    if fn is not None and fn.type == "index_expression":
        # A Go generic instantiation `NewArray[ColStr](…)` /
        # `proto.Null[int32](…)` calls the operand
        fn = fn.child_by_field_name("operand") or fn
    if fn is None:
        return ""
    fn = _unwrapped_callee(fn)  # `await f<T>(x)`, `f!(x)`
    t = fn.type
    if t in {"identifier", "qualified_identifier"}:
        return node_text(fn, source)
    if t == "selector_expression":  # go: pkg.F() / recv.Method()
        fld = fn.child_by_field_name("field")
        return node_text(fld, source) if fld is not None else ""
    if t == "member_expression":  # typescript member call: obj.f() / obj?.f()
        prop = fn.child_by_field_name("property")
        return node_text(prop, source) if prop is not None else ""
    if t == "scoped_identifier":  # rust path call: `Foo::new()`, `crate::a::f()`
        text = node_text(fn, source)
        for prefix in _RUST_PATH_PREFIXES:
            while text.startswith(prefix):
                text = text[len(prefix):]
        head = text.split("::", 1)[0]
        return "" if head in _NOISE_RUST_SCOPES else text
    if t == "generic_function":  # rust `f::<T>()`
        inner = fn.child_by_field_name("function")
        return _call_name_of_callee(inner, source) if inner is not None else ""
    if t == "field_expression":  # cpp / rust member call: obj.f() / obj->f()
        field = fn.child_by_field_name("field")
        return node_text(field, source) if field is not None else ""
    if t == "attribute":  # python member call: obj.f() / self.f()
        attr = fn.child_by_field_name("attribute")
        return node_text(attr, source) if attr is not None else ""
    if t == "template_function":
        name = fn.child_by_field_name("name")
        return node_text(name, source) if name is not None else ""
    return ""


def _receiver_text(call: Any, source: bytes) -> str:
    """The receiver of a TypeScript / JavaScript member call as written —
    `client` in `client.query()`, `this.client` in `this.client.query()`,
    `` for a bare call or a receiver that is itself a call / literal
    (the typer types only names the tree can bind)."""
    if call.type == "type_conversion_expression":  # `proto.Null[int32](v)`
        return _go_generic_conversion_name(call, source)[0]
    fn = call.child_by_field_name("function")
    if fn is not None and fn.type == "attribute":
        # Python `client.query()` / `self.client.query()`
        obj = fn.child_by_field_name("object")
        if obj is None:
            return ""
        if obj.type == "identifier":
            return node_text(obj, source)
        if obj.type == "attribute":
            inner = obj.child_by_field_name("object")
            if (
                inner is not None and inner.type == "identifier"
                and node_text(inner, source) in ("self", "cls")
            ):
                return node_text(obj, source)
            # `a.b.f()` names the module `a.b` the file
            # imported — the whole dotted chain is the receiver when every
            # link is a name (never a call's or a subscript's result)
            text = node_text(obj, source)
            if all(_PY_NAME_RE.match(part) for part in text.split(".")):
                return text
        if obj.type in ("call", "await"):
            # `get_client().query_all()` — the receiver is the
            # call, spelled `get_client()`; its callee's return type types it
            from server.code.domain.extract.py_receivers import callee_text as _py_callee

            callee = _py_callee(obj, source)
            if callee:
                return callee + "()"
        # a call's, a subscript's or a literal's method (`x.encode().decode()`,
        # `rows[0].get()`, `"".join()`): a member call still, never a bare one
        return PY_EXPR_RECEIVER
    if fn is not None and fn.type == "index_expression":
        fn = fn.child_by_field_name("operand") or fn  # `pkg.F[T](…)`: the receiver is pkg
    if fn is not None and fn.type == "field_expression":
        # C++ `obj.f()` / `obj->f()` / `this->f()` — the
        # receiver as written when it is a name or `this`; '' for a call's,
        # a subscript's or an expression's result. As a bare call the C++
        # member call would take the same-file or enclosing-class `f`:
        # `volume->getDisk()` would bind the file's own.
        obj = fn.child_by_field_name("argument")
        if obj is None:
            return ""
        if obj.type in ("identifier", "this"):
            return node_text(obj, source)
        if obj.type == "field_expression":
            inner = obj.child_by_field_name("argument")
            if inner is not None and inner.type == "this":
                return node_text(obj, source)  # `this->member.f()`
        return CPP_EXPR_RECEIVER
    if fn is not None and fn.type == "selector_expression":  # go
        operand = fn.child_by_field_name("operand")
        if operand is None:
            return ""
        from server.code.domain.extract.go_receivers import go_value_spelling

        # `a.store`, `c[]`, `T{}`, `new(T)`, `Conn()`:
        # one spelling shared with the typer's callee keys
        return go_value_spelling(operand, source)
    fn = _unwrapped_callee(fn) if fn is not None else None
    if fn is None or fn.type != "member_expression":
        return ""
    obj = fn.child_by_field_name("object")
    if obj is None or obj.type not in {"identifier", "this", "super", "member_expression"}:
        return ""
    text = node_text(obj, source)
    if text in ("this", "super"):  # `super.m()` names the base's m
        return text
    return text if all(part.isidentifier() for part in text.split(".")) else ""



def _go_package_call(call: Any, source: bytes, aliases: set[str]) -> bool:
    """`cht.New(t)` with `cht` an import alias: the name is the package's,
    however common (`New`, `Close`, `Run`), so it is not language noise. A
    bare `New(x)` or a library's `errors.New` stays noise."""
    if call.type == "type_conversion_expression":
        return _go_generic_conversion_name(call, source)[0] in aliases
    fn = call.child_by_field_name("function")
    if fn is None:
        return False
    if fn.type == "index_expression":
        fn = fn.child_by_field_name("operand") or fn
    if fn.type != "selector_expression":
        return False
    operand = fn.child_by_field_name("operand")
    if operand is None or operand.type != "identifier":
        return False
    return node_text(operand, source) in aliases


def _go_aliases_of(pf: ParsedFile) -> set[str]:
    if pf.lang != "go" or pf.tree is None:
        return set()
    from server.code.domain.extract.lang_go import go_import_aliases

    return set(go_import_aliases(pf))


_CALLEE_WRAPPERS = frozenset(
    {"await_expression", "non_null_expression", "parenthesized_expression"}
)


def _unwrapped_callee(fn: Any) -> Any:
    """Tree-sitter parses `await f<T>(x)` as a call whose
    callee is the await expression, and `f!(x)` / `(f)(x)` likewise wrap the
    callee; the name and receiver are the inner identifier or member."""
    seen = 0
    while fn is not None and fn.type in _CALLEE_WRAPPERS and seen < 3:
        inner = [c for c in fn.named_children if c.type not in ("comment",)]
        if len(inner) != 1 or inner[0].type not in {"identifier", "member_expression",
                                                    *_CALLEE_WRAPPERS}:
            break
        fn = inner[0]
        seen += 1
    return fn


def _call_name_of_callee(fn: Any, source: bytes) -> str:
    fn = _unwrapped_callee(fn)
    if fn.type in {"identifier", "qualified_identifier"}:
        return node_text(fn, source)
    if fn.type == "scoped_identifier":
        text = node_text(fn, source)
        for prefix in _RUST_PATH_PREFIXES:
            while text.startswith(prefix):
                text = text[len(prefix):]
        return "" if text.split("::", 1)[0] in _NOISE_RUST_SCOPES else text
    return ""


def call_sites(pf: ParsedFile) -> list[tuple[int, str]]:
    """(1-indexed line, callee name as written) for every call node in the
    file, in source order, language noise dropped — the input of the callee
    outline: a chunk's own calls are the sites whose line
    falls inside its span. Qualified and member names are kept as written
    (`Poco::Logger::get` stays qualified, `obj->flush()` yields `flush`)."""
    out: list[tuple[int, str]] = []
    if pf.tree is None:
        return out
    source = pf.source
    call_node = _CALL_NODE_BY_LANG.get(pf.lang)
    noise = _NOISE_BY_LANG.get(pf.lang, _NOISE_CPP)
    if call_node is None:
        return out
    go_aliases = _go_aliases_of(pf)

    def walk(node: Any) -> None:
        for child in node.named_children:
            if _is_call(child, call_node, pf.lang):
                name = _call_name(child, source)
                bare = name.split("::")[-1].split(".")[-1]
                if bare and (bare not in noise or _go_package_call(child, source, go_aliases)):
                    out.append((child.start_point[0] + 1, name))  # outline: chain verbs stay noise
            walk(child)

    walk(pf.tree.root_node)
    return out


def collect_call_names(pf: ParsedFile, header: HeaderDecls | None = None) -> set[str]:
    """Bare callee names referenced in this file — the exact set the caller
    should look up via CodeRepo.name_candidates() before extract_edges(),
    so per-merge ingest never loads the whole symbol table. `header`: the
    declarations the ingest resolved for this file, so the
    types they supply are in the table too."""
    names: set[str] = set()
    if pf.tree is None:
        return names
    source = pf.source
    call_node = _CALL_NODE_BY_LANG.get(pf.lang)
    noise = _NOISE_BY_LANG.get(pf.lang, _NOISE_CPP)
    if call_node is None:
        return names
    go_aliases = _go_aliases_of(pf)
    import_keys = _import_keys(pf)

    def walk(node: Any) -> None:
        for child in node.named_children:
            if _is_call(child, call_node, pf.lang):
                name = _call_name(child, source)
                bare = name.split("::")[-1].split(".")[-1]
                if bare and (
                    bare not in noise or _go_package_call(child, source, go_aliases)
                    or _import_exempt(_receiver_text(child, source), bare, import_keys)
                ):
                    names.add(bare)
            walk(child)

    walk(pf.tree.root_node)
    if pf.inherits:
        # A derived class's method names, so the base's rows
        # are in the table for the `overrides` edges and `super.m()` calls
        derived = {d for d, _b in pf.inherits}
        names.update(
            s.qualified_name.rsplit("::", 1)[1] for s in pf.symbols
            if s.kind == "function" and "::" in s.qualified_name
            and s.qualified_name.rsplit("::", 1)[0] in derived
        )
    if pf.lang == "go":
        # The interface method sets and the type rows the
        # implementors derivation joins on — this file's interfaces, its
        # methods (own and interface signatures) and its types by bare name
        for s in pf.symbols:
            if s.kind in ("method", "interface", "struct", "type", "field"):
                names.add(s.name)
            elif s.kind == "function" and s.qualified_name.count("::") >= 2:
                names.add(s.name)
                names.add(s.qualified_name.rsplit("::", 2)[-2])  # the receiver type
        # The fields the file's receiver chains walk
        from server.code.domain.extract.go_receivers import GoReceiverTyper

        typer = GoReceiverTyper(pf)
        for _head, field in typer.chain_fields():
            names.add(field)
        # The struct rows of the types receivers are bound to,
        # whose signatures name the types they embed (cross-file promotion);
        # The consts / vars the file's untyped receivers name
        from server.code.domain.extract.go_receivers import unbound_receiver_heads

        names.update(typer.type_names())
        names.update(unbound_receiver_heads(pf, typer))
    # constructor sites resolve through the constructed type's bare name
    for _line, type_name, _argc in ctor_sites(pf):
        names.add(type_name.split("::")[-1])
    for site in remainder_ctor_sites(pf, header):
        if site.written and not site.decline:
            names.add(site.written.split("::")[-1])
    # base-class names resolve through the same table
    for _derived, base in pf.inherits:
        names.add(base.split("::")[-1])
    # fixture requests resolve through it too (kind-filtered at resolution)
    for _req, fixture_name, _line in pf.fixture_requests:
        names.add(fixture_name)
    # registered classes, their interface and their creators
    if pf.lang == "cpp":
        from server.code.domain.extract import registration

        names.update(registration.registered_names(pf, _file_aliases(pf, header)))
    return names


def _file_aliases(pf: ParsedFile, header: HeaderDecls | None) -> _Aliases:
    """This file's `using` / `typedef` table, the headers' filling the gaps."""
    al = _Aliases()
    if pf.tree is not None:
        al.add_tree(pf.tree.root_node, pf.source)
    if header is not None:
        al.merge(header.aliases)
    return al


def _resolve_fixtures(
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
    local_syms: dict[str, Symbol],
) -> list[Edge]:
    """pytest's documented nearest-first fixture resolution, as a confidence
    ladder over MODULE-SCOPED qualified names (a python qname's first `::`
    component is the dotted file path, so directory distance is string
    arithmetic — no filesystem needed):

        1.0  same module (incl. the requesting file's own conftest-less defs)
        0.9  conftest in the test's own directory
        0.8  conftest in an ancestor directory (nearest one wins)
        0.6  unique kind='fixture' match repo-wide (plugin/helper fixtures)
        drop otherwise — a wrong fixture edge is worse than a missing one
    """
    edges: list[Edge] = []
    if not pf.fixture_requests:
        return edges
    mod = ""
    if pf.symbols:
        mod = pf.symbols[0].qualified_name.split("::", 1)[0]
    mod_dir = mod.rsplit(".", 1)[0] if "." in mod else ""

    local_fixtures: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        if s.kind == "fixture":
            local_fixtures.setdefault(s.name, []).append(s)
    chosen: dict[str, tuple[tuple[str, str, str], float] | None] = {}
    for req_qname, fixture_name, line in pf.fixture_requests:
        requestor = local_syms.get(req_qname)
        if requestor is None:
            continue
        if fixture_name not in chosen:
            chosen[fixture_name] = _nearest_fixture(
                fixture_name, name_table, local_fixtures.get(fixture_name, []), mod, mod_dir
            )
        found = chosen[fixture_name]
        if found is None:
            continue
        best, conf = found
        edges.append(
            Edge(
                "fixture_use", requestor.id, best[0], req_qname,
                best[1], pf.path, line, conf,
            )
        )
    return edges


def _nearest_fixture(
    fixture_name: str, name_table: dict[str, list[tuple[str, str, str]]],
    local: list[Symbol], mod: str, mod_dir: str,
) -> tuple[tuple[str, str, str], float] | None:
    """The fixture a request of `fixture_name` binds and its confidence; None when
    there is none or the nearest rung holds two. Depends on the name only, so a file of
    many requests ranks each name once."""
    cands = [
        c for c in name_table.get(fixture_name, []) if c[2] == "fixture"
    ]
    # same-file fixtures may not be in the repo table yet (first ingest
    # of this file) — synthesize them from the parse itself.
    known = {c[1] for c in cands}
    for s in local:
        if s.qualified_name not in known:
            known.add(s.qualified_name)
            cands.append((s.id, s.qualified_name, "fixture"))
    if not cands:
        return None

    def _rank(c: tuple[str, str, str]) -> tuple[int, int]:
        c_mod = c[1].split("::", 1)[0]
        if c_mod == mod:
            return (0, 0)
        if c_mod == f"{mod_dir}.conftest" and mod_dir:
            return (1, 0)
        if c_mod.endswith(".conftest") or c_mod == "conftest":
            anc = c_mod[: -len(".conftest")] if "." in c_mod else ""
            if mod_dir == anc or mod_dir.startswith(anc + ".") or anc == "":
                # deeper ancestor = nearer = better
                return (2, -len(anc))
        return (3, 0)

    ranked = sorted(cands, key=_rank)
    best = ranked[0]
    tier = _rank(best)[0]
    if tier == 3 and len(cands) != 1:
        return None  # ambiguous non-conftest fixtures: drop, never guess
    # two candidates on the same rung = ambiguity pytest would resolve
    # by plugin order we can't see — drop rather than guess
    if len(ranked) > 1 and _rank(ranked[1]) == _rank(best) and tier != 0:
        return None
    return best, {0: 1.0, 1: 0.9, 2: 0.8, 3: 0.6}[tier]


@dataclass
class CallMiss:
    """A call site the resolver declined: `unresolved` (no definition of
    that name anywhere in the index) or `ambiguous` (`candidates` same-named
    definitions, none pinned by file, enclosing class or qualification)."""

    callee_name: str  # bare
    call_text: str  # as written, e.g. "Foo::bar" or "obj.bar"
    from_symbol_id: str
    from_qualified_name: str
    path: str
    line: int
    disposition: str  # unresolved | ambiguous
    candidates: int = 0


REPO_WIDE_CONFIDENCE = 0.6  # grade of a unique repo-wide name match (rule 3)
# A member call whose receiver the tree types — one fact
# short of the compiler's exact edge, well above the repo-unique guess
TYPED_RECEIVER_CONFIDENCE = 0.9
SAME_PACKAGE_CONFIDENCE = 0.9  # a bare Go call names its own package first
# A call bound through the file's import — the module the specifier resolves to
# holds the one definition (1.0, the reference rule); through a barrel, the one
# definition under its directory
IMPORT_BOUND_CONFIDENCE = 1.0
BARREL_SCOPED_CONFIDENCE = 0.9


class _Namesakes:
    """One name's same-file definitions sorted by qualified name once, so a call site
    counts the ones under a scope (all but its caller's) by bisection: a file can define
    one name thousands of times, and a scan of them per call site is quadratic."""

    def __init__(self, syms: list[Symbol]) -> None:
        self.syms = sorted(syms, key=lambda s: s.qualified_name)  # stable: file order per name
        self.qns = [s.qualified_name for s in self.syms]

    def one(self, prefix: str, skip: str) -> tuple[int, Symbol | None]:
        """How many qualified names start with `prefix`, the ones equal to `skip` left
        out, and that definition when there is exactly one."""
        lo = bisect.bisect_left(self.qns, prefix)
        hi = len(self.qns)
        if prefix:
            hi = bisect.bisect_left(self.qns, prefix[:-1] + chr(ord(prefix[-1]) + 1), lo)
        s_lo = bisect.bisect_left(self.qns, skip, lo, hi)
        s_hi = bisect.bisect_right(self.qns, skip, s_lo, hi)
        n = hi - lo - (s_hi - s_lo)
        if n != 1:
            return n, None
        return 1, self.syms[lo] if lo < s_lo else self.syms[s_hi]


# the rows of a name the table does not hold: one shared list, so the per-list memos of
# `_CallResolver._of` keep one entry for every absent name
_NO_ROWS: list[tuple[str, str, str]] = []


def _first_per_name(pairs: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qn, row in pairs:
        out.setdefault(qn, row)
    return out


def _with_local_rows(
    candidates: list[tuple[str, str, str]], local: list[Symbol],
) -> list[tuple[str, str, str]]:
    """The table's candidates plus the file's own rows for the
    name — an interface's method signature declared in this file is a target
    before its row is read back from the index."""
    known = {c[0] for c in candidates}
    return [*candidates, *((x.id, x.qualified_name, x.kind) for x in local if x.id not in known)]


def _in_scope(qualified_name: str, qualifier: str, bare: str) -> bool:
    """Does `qualified_name` sit in the scope a call named —
    `DB::IStorage::m` for qualifier `IStorage` (a suffix of the scope
    chain, so a namespace-less spelling still matches)."""
    scope = qualified_name[: -len(bare)].rstrip(":") if qualified_name.endswith(bare) else ""
    return bool(scope) and (scope == qualifier or scope.endswith("::" + qualifier))


def _cpp_typer(pf: ParsedFile, callee_returns: dict[str, str] | None) -> Any:
    """The receiver's declared type for a C++ member call —
    parameters, locals, `auto` from a construction, the enclosing class's
    fields (the headers' too, bound by the extraction once it has them)."""
    from server.code.domain.extract.cpp_receivers import CppReceiverTyper

    return CppReceiverTyper(pf, callee_returns)


def candidates_for_call(
    name_table: dict[str, list[tuple[str, str, str]]], call_text: str, bare: str
) -> list[tuple[str, str, str]]:
    """Rule 3's candidate set for one call site: every definition of the bare
    name, narrowed by the qualification the call was written with
    (`Foo::bar` keeps only `...Foo::bar`). Shared with closure repair
    so a site bound outside the extractor is bound by the
    SAME rule the next re-ingest of its file applies — anything else would
    flip-flop between an edge and a miss."""
    candidates = name_table.get(bare, [])
    if "::" in call_text:
        candidates = [
            c for c in candidates if c[1] == call_text or c[1].endswith("::" + call_text)
        ]
    # The two tiers hold ONE definition under two ids (the
    # parser hashes the parameter text, the compiler its own signature);
    # a name is unique when its qualified names are, not its rows. The
    # first row per name survives (the table is sorted, so the choice is
    # stable); reads seed both ids (`tier_ids`), so either binds.
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for c in candidates:
        if c[1] in seen:
            continue
        seen.add(c[1])
        out.append(c)
    return out


def _ctor_matches(qualified_name: str, written: str) -> bool:
    want = f"{written}::{written.split('::')[-1]}"
    return is_constructor_name(qualified_name) and (
        qualified_name == want or qualified_name.endswith("::" + want)
    )


def resolve_ctor(
    caller: Symbol, written: str, argc: int, line: int, *, path: str,
    local: list[Symbol], name_table: dict[str, list[tuple[str, str, str]]],
    declared: int = 0,
) -> tuple[Edge | None, CallMiss | None]:
    """Bind a construction of `written` to `written::T` by the same ladder
    as an ordinary call: same-file unique — narrowed by arity, the local
    overloads carry their parameter lists — at 1.0, unique repo-wide at 0.6.
    Several overloads and no arity match decline as `ambiguous`. A type with
    no indexed constructor (an aggregate, a library type) is no miss: there
    is nothing to bind and nobody asks the graph about it. `local` is the
    file's same-named function symbols; `name_table` the repo-wide table.
    `declared`: how many constructors the class's header
    declares, when a header was read — two or more and the repo-wide rung
    refuses: its candidates carry no signature, so a lone candidate may be
    the wrong overload (the `= default` copy constructor a header defines
    while the real one's source is not indexed yet)."""
    bare = written.split("::")[-1]
    local_all = [s for s in local if s.id != caller.id and _ctor_matches(s.qualified_name, written)]
    fits = [s for s in local_all if _arity_ok(s.signature, argc)]
    if len(fits) == 1:
        target = fits[0]
        return Edge(
            "calls", caller.id, target.id, caller.qualified_name,
            target.qualified_name, path, line, 1.0,
        ), None
    # an overload the arity already ruled out stays ruled out repo-wide
    rejected = {s.id for s in local_all if not _arity_ok(s.signature, argc)}
    candidates = [
        c for c in name_table.get(bare, [])
        if c[2] == "function" and _ctor_matches(c[1], written) and c[0] not in rejected
    ]
    if len(candidates) == 1 and declared < 2:
        cid, cqname = candidates[0][0], candidates[0][1]
        return Edge(
            "calls", caller.id, cid, caller.qualified_name,
            cqname, path, line, REPO_WIDE_CONFIDENCE,
        ), None
    n = len(candidates) + len(local_all)
    if n <= 1:
        return None, None
    return None, CallMiss(
        callee_name=bare, call_text=written[:200], from_symbol_id=caller.id,
        from_qualified_name=caller.qualified_name, path=path, line=line,
        disposition="ambiguous", candidates=n,
    )


# CommonJS names for the module itself: `exports.f()` / `module.exports.f()`
# call the `f` this file defines, whatever `f` is elsewhere
_MODULE_SELF = frozenset({"exports", "module.exports"})

_TYPE_KINDS = frozenset({"class", "struct", "interface", "enum", "type"})
# ECMAScript / DOM / Node globals a class extends without importing them: a
# heritage clause naming one is a library base, never a repository namesake
_SCRIPT_GLOBAL_TYPES = frozenset({
    "Error", "TypeError", "RangeError", "SyntaxError", "ReferenceError", "EvalError",
    "URIError", "AggregateError", "Map", "Set", "WeakMap", "WeakSet", "Promise", "Array",
    "Object", "Function", "Date", "RegExp", "Event", "EventTarget", "HTMLElement",
    "Element", "Node", "CustomEvent", "Transform", "Readable", "Writable", "Duplex",
    "EventEmitter", "Stream", "Component", "PureComponent",
})


def _inherit_edges(
    pf: ParsedFile, name_table: dict[str, list[tuple[str, str, str]]],
    resolver: _CallResolver | None = None,
) -> list[Edge]:
    """inherits: derived class -> base, `extends` and `implements` alike (SCIP
    records both as one implementation relationship). Resolved on the call
    ladder:
    the type this file declares (1.0) -> the type the file imports, at the
    module its specifier resolves to (1.0; 0.9 through a barrel; a
    library's base such as `Error` or `React.Component` binds to nothing) ->
    a unique repo-wide match (0.9 exact qualified name, 0.6 unique bare)."""
    out: list[Edge] = []
    # A Go interface embedding another is heritage too
    cls_syms = {
        s.qualified_name: s for s in pf.symbols if s.kind in {"class", "struct", "interface"}
    }
    local_types: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        if s.kind in _TYPE_KINDS:
            local_types.setdefault(s.name, []).append(s)
    for derived_qname, base_name in pf.inherits:
        derived = cls_syms.get(derived_qname)
        if derived is None:
            continue
        target = _heritage_target(base_name, derived_qname, local_types, name_table, resolver)
        if target is not None:
            out.append(
                Edge(
                    "inherits", derived.id, target[0], derived_qname, target[1],
                    pf.path, derived.start_line, target[2],
                )
            )
    return out


def _caller_spans(pf: ParsedFile, fn_syms: list[Symbol]) -> list[tuple[int, int, Symbol]]:
    """The line spans the call walker locates a call's enclosing unit by,
    widest first per start line. In Python, as in TypeScript, the decorator
    lines above a def belong to the def — `@app.get("/x")`,
    `@pytest.mark.parametrize("v", [make(1)])` are the decorated unit's calls,
    not the file's — as spans of their own, since the def's row keeps the `def`
    line the compiler tier records."""
    spans = [(s.start_line, s.end_line, s) for s in fn_syms]
    if pf.lang == "python" and pf.tree is not None:
        by_start = {s.start_line: s for s in fn_syms}

        def walk(node: Any) -> None:
            for child in node.named_children:
                if child.type == "decorated_definition":
                    inner = child.child_by_field_name("definition")
                    decs = [d for d in child.named_children if d.type == "decorator"]
                    if inner is not None and decs and inner.type == "function_definition":
                        sym = by_start.get(inner.start_point[0] + 1)
                        if sym is not None:
                            spans.append((decs[0].start_point[0] + 1, inner.start_point[0], sym))
                walk(child)

        walk(pf.tree.root_node)
    return sorted(spans, key=lambda t: (t[0], -t[1]))


# the kinds a Python call never binds: bindings, attributes and aliases are
# values, not callables (a `field` holding a callable is the compiler tier's)
_PY_NOT_CALLABLE: frozenset[str] = frozenset({"const", "field", "type"})
# Kinds no call of any language reaches — a googletest body
# (`TEST(Suite, Name)`, kind `test`) is run by the framework, never called.
_NEVER_CALLABLE: frozenset[str] = frozenset({"test"})
# Builtin classes written as receivers (`object.__getattribute__(self, k)`,
# `dict.fromkeys(...)`) — typed, and the repository defines no method on them
_PY_BUILTIN_CLASSES: frozenset[str] = frozenset({
    "object", "str", "bytes", "dict", "list", "set", "frozenset", "tuple", "int", "float",
    "bool", "type", "super", "Exception", "BaseException", "property", "classmethod",
    "staticmethod",
})


_NOT_A_BASE_KIND: frozenset[str] = frozenset({
    "field", "method", "function", "var", "const", "statement", "module",
})


def _fallback_caller(pf: ParsedFile) -> Any:
    """The caller of a site no function holds: the innermost class, else the
    file itself; None outside the script languages, where such a site stays
    dropped."""
    top_level = file_unit(pf)
    if top_level is None:
        return lambda _line: None
    class_at = _class_locator(pf)
    return lambda line: class_at(line) or top_level


def _class_locator(pf: ParsedFile) -> Any:
    """The innermost class holding a line — a call in a class
    body outside any method (a decorator, a field initializer) belongs to
    the class, not the file."""
    spans = sorted(
        ((s.start_line, s.end_line, s) for s in pf.symbols if s.kind in ("class", "struct")),
        key=lambda t: (t[0], -t[1]),
    )

    def class_at(line: int) -> Symbol | None:
        best: Symbol | None = None
        for start, end, s in spans:
            if start <= line <= end:
                best = s
            elif start > line:
                break
        return best

    return class_at


def _heritage_and_overrides(
    pf: ParsedFile, name_table: dict[str, list[tuple[str, str, str]]],
    chains: dict[str, tuple[str, str]] | None,
) -> tuple[list[Edge], dict[str, list[tuple[str, str]]]]:
    """The `inherits` edges (bound through the import rung) plus the
    `overrides` edges they imply, and the bases per derived class the call
    resolver needs for `super.m()`."""
    resolver = _CallResolver(pf, name_table, {}, chains=chains) if pf.inherits else None
    heritage = _inherit_edges(pf, name_table, resolver)
    bases: dict[str, list[tuple[str, str]]] = {}
    for h in heritage:
        bases.setdefault(h.from_qualified_name, []).append((h.to_symbol_id, h.to_qualified_name))
    return [*heritage, *_override_edges(pf, name_table, bases)], bases


def _override_edges(
    pf: ParsedFile, name_table: dict[str, list[tuple[str, str, str]]],
    bases: dict[str, list[tuple[str, str]]],
) -> list[Edge]:
    """`overrides` from a derived method to the same-named
    method of a bound base (same file or the name table), at 1.0 — the
    parser tier's counterpart of the compiler tier's relationship, so the
    callers walk's dispatch widening works without SCIP."""
    if not bases or pf.lang not in ("typescript", "javascript", "python"):
        return []
    local_by_qn = {s.qualified_name: s for s in pf.symbols}
    out: list[Edge] = []
    for s in pf.symbols:
        if s.kind != "function" or "::" not in s.qualified_name:
            continue
        derived, bare = s.qualified_name.rsplit("::", 1)
        if bare in ("constructor", "__init__"):
            continue
        for _bid, base_qn in bases.get(derived, []):
            want = f"{base_qn}::{bare}"
            row = local_by_qn.get(want)
            to_id = row.id if row is not None else next(
                (c[0] for c in name_table.get(bare, []) if c[1] == want), "",
            )
            if to_id:
                out.append(Edge("overrides", s.id, to_id, s.qualified_name, want, pf.path,
                                s.start_line, 1.0))
                break
    return out


def prefer_implementation(
    candidates: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """A declaration file's twin of a name (`types/logger.d.ts`
    beside `lib/log-controller.js`, module scope ending in `.d`) is the
    type of the implementation, not a second definition — when both are
    candidates, the implementation alone remains. A name every candidate
    declares in `.d.ts` files keeps them all (a types-only package)."""
    impl = [c for c in candidates if not c[1].split("::", 1)[0].endswith(".d")]
    return impl if impl and len(impl) < len(candidates) else candidates


def _heritage_target(
    base_name: str, derived_qname: str, local_types: dict[str, list[Symbol]],
    name_table: dict[str, list[tuple[str, str, str]]], resolver: _CallResolver | None,
) -> tuple[str, str, float] | None:
    """(symbol_id, qualified_name, confidence) of a heritage clause's base,
    or None when no rung binds it (a library type, an ambiguous name)."""
    if resolver is not None and resolver.go and "." in base_name:
        # A Go embedded type written `net.Conn` / `st.Base` is
        # the imported package's — outside the repository it binds nothing
        # (never a namesake), inside it binds within that package only
        alias, _, bare = base_name.rpartition(".")
        scope = resolver.go_packages.get(alias)
        if scope == "" or alias not in resolver.go_aliases and scope is None:
            return None
        if scope:
            exact = [
                c for c in name_table.get(bare, [])
                if c[1].startswith(scope + ".") and c[1].endswith("::" + bare)
                and c[1].count("::") == 1
            ]
            return (exact[0][0], exact[0][1], 1.0) if len(exact) == 1 else None
        return None
    bare = base_name.split("::")[-1]
    receiver = base_name.rsplit("::", 1)[0].replace("::", ".") if "::" in base_name else ""
    if resolver is not None and resolver.python and "." in bare:
        # A Python base written `models.Model` / `forms.Field`
        # is the module's class — the receiver is the imported module
        receiver, bare = bare.rsplit(".", 1)
    if receiver == "":
        local = [s for s in local_types.get(bare, []) if s.qualified_name != derived_qname]
        if len(local) == 1:
            return local[0].id, local[0].qualified_name, 1.0
    # a heritage clause names a type: a `field` row spelled by its embedded
    # type's name, a method or a value of that name never
    # qualifies and must not make the type ambiguous
    candidates = [c for c in name_table.get(bare, []) if c[2] not in _NOT_A_BASE_KIND]
    if resolver is not None:
        bound = resolver.import_bound(receiver, bare, candidates)
        if isinstance(bound, tuple):
            return bound
        if bound is False:
            return None  # imported from a library: no repository namesake
    if resolver is not None and resolver.script and bare in _SCRIPT_GLOBAL_TYPES:
        return None  # `extends Error`: the runtime's, whatever the repository names Error
    exact = [c for c in candidates if c[1] == base_name or c[1].endswith("::" + base_name)]
    if resolver is not None and resolver.script:
        exact, candidates = prefer_implementation(exact), prefer_implementation(candidates)
    if len(exact) == 1:
        return exact[0][0], exact[0][1], 0.9
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], 0.6
    return None


def extract_edges(
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
    header: HeaderDecls | None = None,
) -> list[Edge]:
    """Edges for one parsed file. name_table maps bare symbol name ->
    [(symbol_id, qualified_name, kind), ...] repo-wide (both tiers)."""
    edges, _misses = extract_edges_and_misses(pf, name_table, header)
    return edges


class _CallResolver:
    """The call ladder for one file: same-file
    unique → enclosing class (`this`/bare calls) → typed receiver → unique
    repo-wide. Built once per `extract_edges_and_misses`; the walker asks
    `resolve` per call site."""

    def __init__(
        self, pf: ParsedFile, name_table: dict[str, list[tuple[str, str, str]]],
        local_by_name: dict[str, list[Symbol]], callee_returns: dict[str, str] | None = None,
        lane_context: dict[str, Any] | None = None,
        chains: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        # Derived class qualified name -> the (id, qualified
        # name) of each base the heritage pass bound; `super.m()` binds there
        self.bases: dict[str, list[tuple[str, str]]] = {}
        # Local import name -> (declaring module scope, declared name)
        # for names a barrel re-exports; the import rung binds through them at 1.0
        self.chains: dict[str, tuple[str, str]] = chains or {}
        self.path, self.name_table, self.local_by_name = pf.path, name_table, local_by_name
        # Per-name indexes and candidate pools, each built at its first call site
        self._memo: dict[tuple[Any, ...], Any] = {}
        self.local_class_by_name: dict[str, list[Symbol]] = {}
        self.typer: Any = None
        self.bindings, self.go = {}, False  # type: dict[str, str], bool
        self.go_aliases: dict[str, str] = {}
        self.go_packages: dict[str, str] = {}
        self.go_embeds: dict[str, list[str]] = {}
        self.go_own_scope = ""
        self.go_type_aliases: dict[str, str] = {}
        self.python = False
        self.py_bindings: dict[str, tuple[str, str]] = {}
        self.py_star: list[str] = []
        self.py_present: set[str] | None = None
        self.lang = pf.lang
        self.present: set[str] | None = None
        self._go_dirs: frozenset[str] | None = None
        self.local_types: dict[str, list[Symbol]] = {}
        self.py_bases: dict[str, list[str]] = {}
        # The bases of the classes this file names, from
        # their `inherits` rows (`{class qualified name: [base qualified names]}`)
        self.py_class_bases: dict[str, list[str]] = dict(
            (lane_context or {}).get("py_class_bases", {})
        )
        self._language_seat(pf, lane_context)
        self.py_roots: tuple[str, ...] = ()
        self.py_resolved: set[str] = set()
        self.py_init_texts: dict[str, str] = {}
        self.external: set[str] = set()
        self.script = pf.lang in _EXTRA_CALL_NODES_BY_LANG  # typescript / javascript
        if self.script:
            for s in pf.symbols:
                if s.kind == "class":
                    self.local_class_by_name.setdefault(s.name, []).append(s)
            # What the tree says the receiver of a member call is
            from server.code.domain.extract.ts_receivers import ReceiverTyper

            self.typer = ReceiverTyper(pf, callee_returns)
            # A same-file callee's return type needs no read
            same_file = {s.name: s.return_type for s in pf.symbols
                         if s.kind == "function" and s.return_type and "::" not in s.name}
            known = callee_returns or {}
            self.typer.bind_returns({k: v for k, v in same_file.items() if k not in known})
            # What the file imports, by local name, and which
            # of those names no indexed path satisfies (a library's)
            from server.code.domain.extract.lang_typescript_imports import (
                external_names,
                import_bindings,
            )

            self.bindings = import_bindings(pf)
            # a file that resolved no include at all says nothing about which
            # imports are libraries (a parse outside ingest): no name is external
            self.external = external_names(pf, set(pf.includes)) if pf.includes else set()
        elif pf.lang == "cpp":
            self.typer = _cpp_typer(pf, callee_returns)
        elif pf.lang == "python":
            # The same ladder rung for python, typed by the
            # tree's annotations, constructions and `self.<field>` assignments
            from server.code.domain.extract.py_receivers import PyReceiverTyper

            self.typer = PyReceiverTyper(pf, callee_returns)
            known = callee_returns or {}
            self.typer.bind_returns(
                {k: v for k, v in self.typer.same_file_returns().items() if k not in known}
            )
            self._py_seat(pf, lane_context)
        elif pf.lang == "go":
            self._go_seat(pf, callee_returns, lane_context)

    def _language_seat(self, pf: ParsedFile, lane_context: dict[str, Any] | None) -> None:
        """The namespace's present paths (every script
        language's repo-wide rung binds only its own definitions), the
        file's type declarations and heritage clauses (the `super()` rung)."""
        present = (lane_context or {}).get("present")
        self.present = set(present) if present is not None else None
        for s in pf.symbols:
            if s.kind in _TYPE_KINDS:
                self.local_types.setdefault(s.name, []).append(s)
        for derived, base in pf.inherits:
            self.py_bases.setdefault(derived, []).append(base)

    def _py_seat(self, pf: ParsedFile, lane_context: dict[str, Any] | None) -> None:
        """What the file imports, by the name it uses it
        under, and which of those names no indexed path satisfies (the
        standard library's, a dependency's); the namespace's present paths
        when the ingest read them (`py_present`), so a call may bind only a
        Python definition."""
        from server.code.domain.extract.lang_python import (
            external_names,
            py_import_bindings,
            py_star_imports,
        )

        self.python = True
        self.py_bindings = py_import_bindings(pf)
        self.py_star = py_star_imports(pf)
        present = (lane_context or {}).get("py_present")
        self.py_present = set(present) if present is not None else None
        if self.py_present is not None and self.present is None:
            self.present = set(self.py_present)
        # The texts of the packages this file imports from,
        # for the re-export chain (`from fastapi import Depends`)
        self.py_init_texts = dict((lane_context or {}).get("py_init_texts", {}))
        # The roots the checkout's manifests declare for this importer
        self.py_roots = tuple((lane_context or {}).get("py_roots") or ())
        # the paths the ingest resolved the file's specifiers to (a parse
        # outside the ingest still holds the raw specifiers — `lib`, not a
        # path — and resolved nothing: then no name is external and the
        # rung keeps climbing)
        self.py_resolved = {p for p in pf.includes if p.endswith((".py", ".pyi"))}
        if self.py_present is not None:
            self.py_resolved |= self.py_present
        self.external = (
            external_names(pf, self.py_resolved, self.py_roots) if self.py_resolved else set()
        )

    def _py_path_of(self, spec: str) -> str:
        """The present path a specifier names, '' when none does."""
        from server.code.domain.extract.lang_python import first_present_candidate

        return self._once(("path of", spec), lambda: first_present_candidate(
            self.path, spec, self.py_resolved, self.py_roots
        ))

    def _py_scope_bound(
        self, spec: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None:
        """The module-level definition `bare` of the module `spec` names
        (1.0); for a package (`__init__.py`) that does not define it, the one
        definition of that name under the package's directory (0.9: the
        package re-exports it). None when the specifier names no present
        path or the module has no such definition."""
        from server.code.domain.extract.lang_python import module_scope

        path = self._py_path_of(spec)
        if not path:
            return None
        scope = module_scope(path)
        exact = self._pairs(candidates, f"{scope}::{bare}")
        if len(exact) == 1:
            cid, cqn = next(iter(exact))
            return cid, cqn, IMPORT_BOUND_CONFIDENCE
        chained = self._py_reexport_bound(path, bare, candidates)  # any module re-exports
        if chained is not None:
            return chained
        if path.endswith("/__init__.py"):
            pkg = scope[: -len(".__init__")] if scope.endswith(".__init__") else scope
            under = self._of(candidates, ("under", pkg, bare), lambda: {
                (c[0], c[1]) for c in candidates
                if c[1].startswith(pkg + ".") and c[1].endswith("::" + bare)
                and c[1].count("::") == 1
            })
            if len(under) == 1:
                cid, cqn = next(iter(under))
                return cid, cqn, BARREL_SCOPED_CONFIDENCE
        return None

    def _py_reexport_bound(
        self, init_path: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None:
        """The package's `__init__.py` says where `bare` comes
        from (`from .param_functions import Depends as Depends`); the
        definition there is the binding (1.0). Needs the package texts the
        ingest prefetched (`py_init_texts`); star re-exports narrow to the
        starred modules."""
        from server.code.domain.extract.lang_python import module_scope, py_reexport_target

        if not self.py_init_texts:
            return None
        target = py_reexport_target(init_path, bare, self.py_init_texts, self.py_resolved)
        if target is None:
            return None
        if isinstance(target, list):
            hits = {
                pair for scope in {module_scope(t) for t in target} if "::" not in scope
                for pair in self._pairs(candidates, f"{scope}::{bare}")
            }
        else:
            tpath, declared = target
            if not declared:
                return None  # a submodule: nothing to call
            hits = self._pairs(candidates, f"{module_scope(tpath)}::{declared}")
        if len(hits) == 1:
            cid, cqn = next(iter(hits))
            return cid, cqn, IMPORT_BOUND_CONFIDENCE
        return None

    def _py_class_member_bound(
        self, spec: str, cls: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None:
        """`K.m()` where `K` was imported from `spec`: the method `m` of the
        class `K` that module (or its package) defines (1.0)."""
        from server.code.domain.extract.lang_python import module_scope

        path = self._py_path_of(spec)
        if not path:
            return None
        scope = module_scope(path)
        pkg = scope[: -len(".__init__")] if scope.endswith(".__init__") else scope
        hits = {
            (c[0], c[1]) for c in self._member_rows(candidates, cls, bare)
            if c[1].startswith(scope + "::") or c[1].startswith(pkg + ".")
        }
        if len(hits) == 1:
            cid, cqn = next(iter(hits))
            return cid, cqn, IMPORT_BOUND_CONFIDENCE
        return None

    def _py_spec_external(self, spec: str) -> bool:
        """An absolute specifier no present path satisfies is a library's —
        when the file's imports were resolved at all; a relative one always
        names the repository."""
        if spec.startswith(".") or not self.py_resolved:
            return False
        return not self._py_path_of(spec)

    def py_import_bound(
        self, receiver: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None | bool:
        """The import rung for Python. A bare `f()` whose `f`
        the file imported (`from m import f [as f]`) binds to the definition
        at the module the specifier resolves to (1.0), or to the one
        definition of that name under the package the specifier names (0.9);
        `m.f()` / `a.b.f()` whose receiver is an imported module binds the
        same way; `K.m()` whose `K` was imported binds the class's method;
        a name from a star import binds to the one definition among the
        starred modules (0.9). A receiver or name imported from a module no
        indexed path satisfies is a library's and binds nothing: `False`
        (stop). None: the rung does not apply — keep climbing."""
        from server.code.domain.extract.lang_python import join_spec

        if receiver == "":
            b = self.py_bindings.get(bare)
            if b is None:
                hits = {
                    r for spec in self.py_star
                    for r in [self._py_scope_bound(spec, bare, candidates)] if r is not None
                }
                if len(hits) == 1:
                    cid, cqn, _conf = next(iter(hits))
                    return cid, cqn, BARREL_SCOPED_CONFIDENCE
                return None
            spec, member = b
            if member == "":
                # `import m as f; f()` — a module is not callable; `f` shadows
                # any repository namesake either way
                return False
            if member != bare:
                # `from m import f as g; g()` — the definitions are named `f`
                candidates = self._table(member, member)
            bound = self._py_scope_bound(spec, member, candidates)
            if bound is not None:
                return bound
            if self._py_spec_external(spec):
                return False
            return None
        parts = receiver.split(".")
        if parts[0] in ("self", "cls"):
            return None
        for i in range(len(parts), 0, -1):
            b = self.py_bindings.get(".".join(parts[:i]))
            if b is None:
                continue
            spec, member = b
            tail = parts[i:]
            if member:
                # `from a import sub; sub.f()` — a submodule first, then a class
                mod_spec = join_spec(spec, ".".join([member, *tail]))
                bound = self._py_scope_bound(mod_spec, bare, candidates)
                if bound is not None:
                    return bound
                if not tail:
                    bound = self._py_class_member_bound(spec, member, bare, candidates)
                    if bound is not None:
                        return bound
            else:
                mod_spec = join_spec(spec, ".".join(tail)) if tail else spec
                bound = self._py_scope_bound(mod_spec, bare, candidates)
                if bound is not None:
                    return bound
                if tail:
                    cls_spec = join_spec(spec, ".".join(tail[:-1])) if len(tail) > 1 else spec
                    bound = self._py_class_member_bound(cls_spec, tail[-1], bare, candidates)
                    if bound is not None:
                        return bound
            # the receiver is an import: a library's binds nothing; a
            # repository module's without this name keeps climbing (a
            # re-export the rung above did not see)
            return False if self._py_spec_external(spec) else None
        return None

    def _py_library_call(self, receiver: str, bare: str, line: int) -> bool:
        """A member call on a receiver the tree cannot type
        whose name is a builtin or standard-library method's (`decode`,
        `exists`, `parse_args`) binds no repository namesake repo-wide. A
        typed receiver was bound above; an imported module's call was bound
        or stopped by the import rung."""
        from server.code.domain.extract.py_library_methods import PY_LIBRARY_METHODS

        if receiver == PY_EXPR_RECEIVER:
            return bare in PY_LIBRARY_METHODS
        if self._py_receiver_type(line, receiver):
            # the receiver's type is known and the repository defines no such
            # method on it (the typed rung ran first): a library class's
            # method (`parser.parse_args()` on an ArgumentParser), or a
            # method inherited from one — never a repository namesake
            return True
        return bare in PY_LIBRARY_METHODS

    def _named_type_target(
        self, tname: str, bare: str, candidates: list[tuple[str, str, str]], receiver: str = "",
    ) -> tuple[str, str] | None:
        """The one `…::<tname>::<bare>` the table holds (TypeScript, Python);
        for Python, two classes of the name are told apart by the import."""
        narrowed = self._member_rows(candidates, tname, bare)
        if len(narrowed) > 1 and self.python:
            wide = narrowed
            narrowed = self._of(
                wide, ("by class", tname), lambda: self._py_narrow_by_class(tname, wide)
            )
        if len(narrowed) > 1 and self.python:
            near = narrowed
            anchor = self._py_anchor(receiver)
            narrowed = self._of(
                near, ("nearest", anchor), lambda: self._py_nearest_module(near, anchor)
            )
        if len(narrowed) != 1:
            return None
        return narrowed[0][0], narrowed[0][1]

    def _py_anchor(self, receiver: str) -> str:
        """The path a receiver's defining module is read from: its import's, else this
        file's."""
        b = self.py_bindings.get(receiver.split(".", 1)[0]) if receiver else None
        if b is not None:
            defined_in = self._py_path_of(b[0])
            if defined_in:
                return defined_in
        return self.path

    def _py_nearest_module(
        self, narrowed: list[tuple[str, str, str]], anchor: str,
    ) -> list[tuple[str, str, str]]:
        """Two vendored copies of one class (`AirflowConfigParser`
        under `airflow-core/src/airflow/_shared` and `task-sdk/src/airflow/
        sdk/_shared`) — the one whose module shares the longest path with
        the receiver's defining module (an imported `conf` lives in
        `airflow-core/src/airflow/configuration.py`), else with this file,
        wins; a tie stands."""
        from server.code.domain.extract.lang_python import module_scope

        here = module_scope(anchor).split(".")

        def shared(c: tuple[str, str, str]) -> int:
            parts = c[1].split("::", 1)[0].split(".")
            n = 0
            for a, b in zip(here, parts, strict=False):
                if a != b:
                    break
                n += 1
            return n

        best = max(shared(c) for c in narrowed)
        hits = [c for c in narrowed if shared(c) == best]
        return hits if len(hits) == 1 and best > 0 else narrowed

    def _py_receiver_type(self, line: int, receiver: str) -> str:
        """The receiver's named type: a name's binding at `line`, or a call
        receiver's (`f()`) callee return type."""
        if self.typer is None or not receiver:
            return ""
        as_class = self._py_class_object(receiver)
        if as_class:
            return as_class
        return str(self.typer.type_of_expr(line, receiver) or "")  # chains too

    def _py_narrow_by_class(
        self, tname: str, narrowed: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        """Two classes of one name (`_CHClient` in
        `core.repo_base` and in `memory.domain.search_types`) — the receiver's
        type is the one this file imports under that name, else the one this
        file defines; otherwise the ambiguity stands."""
        from server.code.domain.extract.lang_python import module_scope

        scopes: list[str] = []
        b = self.py_bindings.get(tname)
        if b is not None:
            spec, member = b
            path = self._py_path_of(spec) if member else ""
            if path:
                scope = module_scope(path)
                scopes.append(scope)
                if path.endswith("/__init__.py"):
                    pkg = scope[: -len(".__init__")] if scope.endswith(".__init__") else scope
                    scopes.append(pkg)
        elif tname in self.local_class_by_name or self._local_class_name(tname):
            scopes.append(module_scope(self.path))
        for scope in scopes:
            hits = [
                c for c in narrowed
                if c[1].split("::", 1)[0] == scope or c[1].startswith(scope + ".")
            ]
            if len(hits) == 1:
                return hits
        return narrowed

    def same_language_definition(self, qualified_name: str) -> bool:
        """Is this script-class definition the caller's
        language's, read off the present paths (True when unknown)."""
        if self.present is None:
            return True
        from server.code.domain.extract.name_scope import defined_in_language, go_package_dirs

        if self.lang == "go" and self._go_dirs is None:
            self._go_dirs = go_package_dirs(self.present)
        return defined_in_language(qualified_name, self.lang, self.present, self._go_dirs)

    def py_super_target(self, caller: Symbol, bare: str, line: int) -> Edge | None:
        """`super().m()` inside a method names the first base
        class of the enclosing class that defines `m` — the base bound the
        way a heritage clause is (same file, import, unique), the method
        under it exactly. Confidence follows the base's binding, capped at
        the typed rung's. One level: a grandparent's method is the compiler
        tier's."""
        cls_qn = caller.qualified_name.rsplit("::", 1)[0]
        if "::" not in cls_qn:
            return None
        return self.py_inherited_target(caller, cls_qn, bare, line)

    def py_inherited_target(
        self, caller: Symbol, cls_qn: str, bare: str, line: int, depth: int = 4,
    ) -> Edge | None:
        """The first base of `cls_qn`, in declaration order and
        then their bases (a same-file hierarchy is walked; a base declared in
        another file stops the walk there), that defines `bare`."""
        for base in self.py_bases.get(cls_qn, []):
            target = _heritage_target(base, cls_qn, self.local_types, self.name_table, self)
            if target is None:
                continue
            found = self._py_member_of(caller, target[1], bare, line, target[2], depth)
            if found is not None:
                return found
        # a class declared in another file: its bases from its rows
        if cls_qn not in self.py_bases:
            for base_qn in self.py_class_bases.get(cls_qn, []):
                found = self._py_member_of(caller, base_qn, bare, line, 0.9, depth)
                if found is not None:
                    return found
        return None

    def _py_member_of(
        self, caller: Symbol, cls_qn: str, bare: str, line: int, conf: float, depth: int,
    ) -> Edge | None:
        """`cls_qn`'s own `bare`, else (depth permitting) its bases'."""
        wanted = f"{cls_qn}::{bare}"
        hits = self._by_qn(self.name_table.get(bare, _NO_ROWS)).get(wanted, [])
        if len(hits) == 1:
            return Edge(
                "calls", caller.id, hits[0][0], caller.qualified_name, hits[0][1],
                self.path, line, min(conf, TYPED_RECEIVER_CONFIDENCE),
            )
        if depth > 1 and (cls_qn in self.py_bases or cls_qn in self.py_class_bases):
            return self.py_inherited_target(caller, cls_qn, bare, line, depth - 1)
        return None

    def _py_nested_def(self, caller: Symbol, local: _Namesakes) -> Symbol | None:
        """The one candidate defined inside the caller, else inside the
        nearest enclosing function of the caller (a sibling closure)."""
        scope = caller.qualified_name
        while "::" in scope:
            n, inside = local.one(scope + "::", caller.qualified_name)
            if n == 1:
                return inside
            if n:
                return None  # several closures of the name in one scope: ambiguous
            scope = scope.rsplit("::", 1)[0]
            if self._local_class(scope):
                return None  # crossed into the class body: rung 2's ground
        return None

    def _py_method_qualified(self, qualified_name: str) -> bool:
        """The qualified name of a method of a class the name
        table holds (`pkg.frame::Frame::merge`), file-independent."""
        if qualified_name.count("::") < 2:
            return False
        parent = qualified_name.rsplit("::", 1)[0]
        return any(
            kind == "class"
            for _sid, _qn, kind in self._by_qn(
                self.name_table.get(parent.rsplit("::", 1)[-1], _NO_ROWS)
            ).get(parent, ())
        )

    def _once[T](self, key: tuple[Any, ...], build: Callable[[], T]) -> T:
        if key not in self._memo:
            self._memo[key] = build()
        return cast(T, self._memo[key])

    def _local(self, bare: str) -> _Namesakes:
        """The file's callable definitions of `bare`."""
        return self._once(("local", bare), lambda: _Namesakes([
            s for s in self.local_by_name.get(bare, []) if s.kind not in _NEVER_CALLABLE
        ]))

    def _table(self, name: str, bare: str) -> list[tuple[str, str, str]]:
        """`candidates_for_call`, once per call text; callers never change the list."""
        return self._once(
            ("table", name, bare), lambda: candidates_for_call(self.name_table, name, bare)
        )

    def _of[T](self, rows: list[Any], key: tuple[Any, ...], build: Callable[[], T]) -> T:
        """`build()` once per candidate list and `key`; the entry holds the list, so its id
        is never another list's."""
        held = self._memo.get(("of", id(rows), *key))
        if held is None:
            held = self._memo[("of", id(rows), *key)] = (rows, build())
        return cast(T, held[1])

    def _by_qn(self, rows: list[tuple[str, str, str]]) -> dict[str, list[tuple[str, str, str]]]:
        """The rows per qualified name, in order."""
        def build() -> dict[str, list[tuple[str, str, str]]]:
            out: dict[str, list[tuple[str, str, str]]] = {}
            for c in rows:
                out.setdefault(c[1], []).append(c)
            return out

        return self._of(rows, ("qn",), build)

    def _pairs(self, rows: list[tuple[str, str, str]], qn: str) -> set[tuple[str, str]]:
        return {(c[0], c[1]) for c in self._by_qn(rows).get(qn, ())}

    def _by_member(
        self, rows: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
        """The rows `…::<owner>::<name>` per (owner, name), in order: what an
        `endswith(f"::{owner}::{name}")` filter keeps, for an owner and name without `:`."""
        def build() -> dict[tuple[str, str], list[tuple[str, str, str]]]:
            out: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
            for c in rows:
                parts = c[1].rsplit("::", 2)
                if len(parts) == 3:
                    out.setdefault((parts[1], parts[2]), []).append(c)
            return out

        return self._of(rows, ("member",), build)

    def _member_rows(
        self, rows: list[tuple[str, str, str]], owner: str, name: str,
    ) -> list[tuple[str, str, str]]:
        """`[c for c in rows if c[1].endswith(f"::{owner}::{name}")]`."""
        if ":" in owner or ":" in name:
            return self._of(rows, ("suffix", owner, name), lambda: [
                c for c in rows if c[1].endswith(f"::{owner}::{name}")
            ])
        return self._by_member(rows).get((owner, name), [])

    def _local_rows(self, bare: str) -> dict[str, Symbol]:
        """The file's first definition of `bare` per qualified name."""
        return self._once(("local rows", bare), lambda: _first_per_name(
            (s.qualified_name, s) for s in self.local_by_name.get(bare, [])
        ))

    def _table_rows(self, bare: str) -> dict[str, tuple[str, str, str]]:
        """The name table's first row of `bare` per qualified name."""
        return self._once(("table rows", bare), lambda: _first_per_name(
            (c[1], c) for c in self.name_table.get(bare, [])
        ))

    def _local_class_name(self, name: str) -> bool:
        """Does the file's name index hold a class row under `name`?"""
        names = self._once(("class names",), lambda: {
            n for n, rows in self.local_by_name.items() if any(s.kind == "class" for s in rows)
        })
        return name in names

    def miss(self, caller: Symbol, name: str, bare: str, line: int) -> CallMiss:
        """The declined-site record for one call: `ambiguous` when several
        definitions of the bare name exist (repo-wide or same-file), else
        `unresolved`."""
        local = self._once(
            ("all local", bare), lambda: _Namesakes(self.local_by_name.get(bare, []))
        )
        n = len(self._table(name, bare)) + local.one("", caller.qualified_name)[0]
        return CallMiss(
            callee_name=bare, call_text=name[:200], from_symbol_id=caller.id,
            from_qualified_name=caller.qualified_name, path=self.path, line=line,
            disposition="ambiguous" if n > 1 else "unresolved", candidates=n,
        )

    def _local_class(self, qualified_name: str) -> bool:
        """Is this the qualified name of a class this file defines?"""
        rows = self._once(("classes",), lambda: {
            (t.name, t.qualified_name)
            for ts in self.local_types.values() for t in ts if t.kind == "class"
        })
        return (qualified_name.rsplit("::", 1)[-1], qualified_name) in rows

    def _py_is_method(self, s: Symbol) -> bool:
        parent = s.qualified_name.rsplit("::", 1)[0] if "::" in s.qualified_name else ""
        return self._local_class(parent)

    def _py_class_object(self, receiver: str) -> str:
        """A receiver that is a class written by name —
        `Klass.method()` on a class this file defines, or a builtin class
        (`object.__getattribute__`, `dict.fromkeys`) — is typed as that
        class; '' otherwise (an imported class binds through the import)."""
        if not receiver or "." in receiver or receiver[:1].islower():
            return ""
        if receiver in _PY_BUILTIN_CLASSES:
            return receiver
        if self._local_class_name(receiver):
            return receiver
        return ""  # an imported class is the import rung's (1.0, `_py_class_member_bound`)

    def py_definition(self, qualified_name: str) -> bool:
        """Is this script-class definition a Python one? Read off the paths
        its module scope can come from against the present set; True when
        the set is unknown (a parse outside the ingest)."""
        if self.py_present is None:
            return True
        from server.code.domain.extract.lang_python import def_paths_of_scope
        from server.code.domain.extract.name_scope import SCRIPT, qualified_name_class

        if qualified_name_class(qualified_name) != SCRIPT:
            return True
        scope = qualified_name.split("::", 1)[0]
        a, b = def_paths_of_scope(scope)
        return a in self.py_present or b in self.py_present

    def _go_seat(
        self, pf: ParsedFile, callee_returns: dict[str, str] | None,
        lane_context: dict[str, Any] | None,
    ) -> None:
        """The Go typer and package facts, read from the lane context the
        ingest built."""
        # The same rung for Go — receivers, parameters,
        # composite literals, constructor returns, struct fields — and
        # the file's imports by the name it uses them under
        from server.code.domain.extract.go_receivers import GoReceiverTyper
        from server.code.domain.extract.lang_go import go_import_aliases

        self.go = True
        self.typer = GoReceiverTyper(pf, callee_returns)
        known = callee_returns or {}
        self.typer.bind_returns(
            {k: v for k, v in self.typer.same_file_returns().items() if k not in known}
        )
        self.go_aliases = go_import_aliases(pf)
        # alias -> package directory the ingest resolved through the module
        # index ('' = a package outside the repository: stdlib, a dependency)
        self.go_packages = dict((lane_context or {}).get("go_packages", {}))
        # A package is written under its CLAUSE (`ch` for
        # …/ch-go, whose path element says ch_go): the ingest read the
        # clauses of the imported directories from the name table
        for clause, pkg_dir in (lane_context or {}).get("go_clauses", {}).items():
            if clause not in self.go_packages:
                self.go_packages[clause] = pkg_dir
                self.go_aliases.setdefault(clause, "")
        self.typer._import_aliases.update((lane_context or {}).get("go_clauses", {}))
        # The rows the ingest read for this file —
        # method signatures and field types — by symbol id
        self.go_sigs: dict[str, str] = dict((lane_context or {}).get("go_method_sigs", {}))
        self.typer.field_lookup = self.go_field_type
        for derived, base in pf.inherits:
            self.go_embeds.setdefault(derived.rsplit("::", 1)[-1], []).append(base)
        # Structs declared in other files, from their rows
        for qn, embeds in (lane_context or {}).get("go_struct_embeds", {}).items():
            listed = self.go_embeds.setdefault(qn.rsplit("::", 1)[-1], [])
            listed.extend(e for e in embeds if e not in listed)
        # The rows the ingest read for this file's untyped
        # receiver heads and named containers
        self.typer.type_elems = dict((lane_context or {}).get("go_type_elems", {}))
        self.typer.bind_values(dict((lane_context or {}).get("go_value_types", {})))
        # An unqualified type is THIS package's — `Client`
        # in package ch is never chpool's Client
        self.go_own_scope = next((s.qualified_name.partition("::")[0] for s in pf.symbols), "")
        # Type aliases (`type Dynamic = Variant`), this
        # file's and the rows', by qualified name → target as written
        from server.code.domain.extract.go_implementors import type_alias_target

        self.go_type_aliases = {
            s.qualified_name: type_alias_target(s.signature)
            for s in pf.symbols if s.kind == "type" and type_alias_target(s.signature)
        }
        self.go_type_aliases.update((lane_context or {}).get("go_type_aliases", {}))

    def import_bound(
        self, receiver: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None | bool:
        """Rung 2c (scope-graph style resolution): a bare `f()` whose `f`
        the file imports, or `utils.f()` whose `utils` is a namespace import,
        binds to the definition at the module the specifier resolves to
        (1.0); through a barrel (`../src` → `src/index.ts`) to the one
        definition under the barrel's directory (0.9). A name imported from
        a library resolves to no indexed module and must never bind to a
        repository namesake: that is `False` (stop), where None means "the
        rung does not apply — keep climbing"."""
        if self.python:
            return self.py_import_bound(receiver, bare, candidates)
        if not self.script or not self.bindings:
            return None
        key = bare if receiver == "" else receiver if "." not in receiver else ""
        spec = self.bindings.get(key, "") if key else ""
        if not spec:
            return None
        return self._of(candidates, ("ts import", key, bare), lambda: self._ts_import_bound(
            key, spec, bare, candidates
        ))

    def _ts_import_bound(
        self, key: str, spec: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None | bool:
        from server.code.domain.extract.lang_typescript_refs import (
            _import_bound,
            _is_barrel_spec,
            under_barrel,
        )

        bound = _import_bound(self.path, spec, candidates)
        if bound is not None:
            return bound[0], bound[1], IMPORT_BOUND_CONFIDENCE
        chained = self.chain_bound(key, bare, candidates)
        if chained is not None:
            return chained
        if key in self.external:
            return False  # a library's name, whatever the repository calls its own
        if _is_barrel_spec(self.path, spec, candidates):
            scoped = under_barrel(self.path, spec, candidates)
            if len({c[1] for c in scoped}) == 1:
                return scoped[0][0], scoped[0][1], BARREL_SCOPED_CONFIDENCE
        return None  # resolvable import, module not matched (a re-export chain): keep climbing

    def chain_bound(
        self, key: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None:
        """The name reached its barrel through a re-export chain
        the ingest followed to the declaring module; the definition there,
        under the declared name, is the binding (1.0). `key` is the local
        name the import bound; for a namespace root the member's bare name
        is looked up in the chain's module."""
        chain = self.chains.get(key)
        if chain is None:
            return None
        module, declared = chain
        wanted = declared if key == bare else bare
        pool = candidates if wanted == bare else self.name_table.get(wanted, [])
        hits = {(c[0], c[1]) for c in pool
                if c[1].split("::", 1)[0] == module and c[1].split("::")[-1] == wanted}
        if len(hits) == 1:
            cid, cqn = next(iter(hits))
            return cid, cqn, IMPORT_BOUND_CONFIDENCE
        return None

    @staticmethod
    def field_based(
        receiver: str, candidates: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        """Rung 3's candidate set for a script call, ordered the way the
        language reads it (a field-based call graph): a member call `obj.f()` names a PROPERTY,
        so member definitions (`Cls::f`, a prototype method) come first and
        free functions count only when no member is named `f` — the CommonJS
        idiom `lib.f()` / a function decorated onto an instance
        (`fastify.getDefaultJsonParser()`) is a call to the free `f`. A bare
        `f()` names a binding in scope: free definitions first, members only
        when there is no free `f` (a destructured method)."""
        def is_member(qn: str) -> bool:
            return qn.count("::") >= 2

        members = [c for c in candidates if is_member(c[1])]
        free = [c for c in candidates if not is_member(c[1])]
        first, second = (free, members) if receiver == "" else (members, free)
        return first if first else second

    def _class_of(self, caller: Symbol) -> str:
        return caller.qualified_name.rsplit("::", 1)[0] if "::" in caller.qualified_name else ""

    def _super_target(self, caller: Symbol, bare: str, line: int) -> Edge | None:
        """The base method a `super.m()` names, at 1.0 when a
        bound base declares `m`; None otherwise (the walker declines the
        site when no base is bound — a library base is nobody's miss)."""
        for _bid, base_qn in self.bases.get(self._class_of(caller), []):
            want = f"{base_qn}::{bare}"
            row = self._local_rows(bare).get(want)
            if row is not None:
                return Edge("calls", caller.id, row.id, caller.qualified_name, want,
                            self.path, line, 1.0)
            cand = self._table_rows(bare).get(want)
            if cand is not None:
                return Edge("calls", caller.id, cand[0], caller.qualified_name, want,
                            self.path, line, 1.0)
        return None

    def declined(self, receiver: str, line: int) -> bool:
        """`super.m()` inside a class whose base the index
        does not hold (a library base) is nobody's miss."""
        if receiver == "super" and not self.go:
            classes = [
                x for rows in self.local_class_by_name.values() for x in rows
                if x.start_line <= line <= x.end_line
            ]
            cls = min(classes, key=lambda x: x.end_line - x.start_line, default=None)
            return cls is None or not self.bases.get(cls.qualified_name)
        return self._declined_package(receiver, line)

    def _declined_package(self, receiver: str, line: int) -> bool:
        """A Go call on a package the repository does not
        hold (`fmt.Println`, `os.Exit`) or on a receiver typed by such a
        package (`t.Run` with `t *testing.T`) is nobody's miss: the callee is
        the standard library's or a dependency's."""
        if not self.go or not receiver:
            return False
        head = receiver.split(".", 1)[0]
        if head in self.go_aliases:
            return not self.go_packages.get(head, "")
        tname = self.typer.type_of(line, receiver) if self.typer is not None else ""
        if "." in tname:
            alias = tname.split(".", 1)[0]
            return alias in self.go_aliases and not self.go_packages.get(alias, "")
        return False

    def go_field_type(self, type_written: str, field: str) -> str:
        """The written type of `field` on the type `type_written` (`st.Store`
        or `Store`), from the repository's `field` rows whose signature the
        ingest read; '' when unknown or ambiguous."""
        alias, _, type_bare = type_written.rpartition(".")
        scope = self.go_packages.get(alias) if alias else None
        if alias and scope == "":
            return ""
        rows = [
            c for c in self.name_table.get(field, [])
            if c[2] == "field" and c[1].endswith(f"::{type_bare}::{field}")
            and (not scope or c[1].startswith(scope + "."))
        ]
        if len(rows) != 1:
            return ""
        written = self.go_sigs.get(rows[0][0], "")
        # A field typed by a bare name inside package `alias`
        # names that package's type (`store *Store` on st's app is st.Store)
        if alias and written and "." not in written and written[0].isupper():
            return f"{alias}.{written}"
        return written

    def go_package_bound(
        self, receiver: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None | bool:
        """`st.NewStore()` where `st` imports a package the
        module index places at a repository directory binds to that
        package's own `NewStore` (1.0); an import outside every module is
        `False` (stop: a library's name, whatever the repository calls its
        own); anything else keeps climbing."""
        if not self.go or not receiver or "." in receiver or receiver not in self.go_aliases:
            return None
        scope = self.go_packages.get(receiver)
        if scope is None:
            return None  # the alias is known, its package not resolved (a parse outside ingest)
        if not scope:
            return False
        return self._in_package(scope, bare, candidates)

    def _in_package(
        self, scope: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None:
        exact = self._of(candidates, ("package", scope, bare), lambda: [
            c for c in candidates
            if c[1].startswith(scope + ".") and c[1].count("::") == 1 and c[1].endswith("::" + bare)
        ])
        if len(exact) == 1:
            return exact[0][0], exact[0][1], IMPORT_BOUND_CONFIDENCE
        return None

    def go_clause_bound(
        self, receiver: str, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None:
        """`clickhouse.Open()` where no import is named
        `clickhouse` — the package clause of an imported package differs from
        its path's last element (`clickhouse-go/v2` declares `package
        clickhouse`). A definition whose scope ends in `.<receiver>` under
        the directory of a package this file imports is that package's."""
        if not self.go or not receiver or "." in receiver or receiver in self.go_aliases:
            return None
        dirs = {d for d in self.go_packages.values() if d}
        if not dirs:
            return None
        found = [
            c for c in self._by_clause(candidates, bare).get(receiver, [])
            if c[1].partition("::")[0].rsplit(".", 1)[0] in dirs
        ]
        if len(found) == 1:
            return found[0][0], found[0][1], IMPORT_BOUND_CONFIDENCE
        return None

    def _by_clause(
        self, candidates: list[tuple[str, str, str]], bare: str,
    ) -> dict[str, list[tuple[str, str, str]]]:
        """The package-level definitions `<dir>.<clause>::<bare>` per clause, in order."""
        def build() -> dict[str, list[tuple[str, str, str]]]:
            out: dict[str, list[tuple[str, str, str]]] = {}
            for c in candidates:
                scope, _, rest = c[1].partition("::")
                if rest == bare and "." in scope:
                    out.setdefault(scope.rsplit(".", 1)[1], []).append(c)
            return out

        return self._of(candidates, ("clause", bare), build)

    def _clause_dirs(self, candidates: list[tuple[str, str, str]], clause: str) -> set[str]:
        """The directories of the candidates' packages whose clause is `clause`."""
        return self._of(candidates, ("clause dirs", clause), lambda: {
            c[1].partition("::")[0].rpartition(".")[0] for c in candidates
            if c[1].partition("::")[0].endswith(f".{clause}")
        })

    def go_same_package(
        self, caller: Symbol, bare: str, candidates: list[tuple[str, str, str]],
    ) -> tuple[str, str, float] | None:
        """A bare call names the caller's own package before
        the repository — `GetStdDSNConnection()` in `tests/std` is
        `tests.std.std::GetStdDSNConnection`, whatever `examples/std` calls its
        helper. One definition in the package binds at 0.9."""
        if not self.go:
            return None
        scope = caller.qualified_name.partition("::")[0]
        own = self._by_qn(candidates).get(f"{scope}::{bare}", [])
        if len(own) == 1:
            return own[0][0], own[0][1], SAME_PACKAGE_CONFIDENCE
        return None

    def typed_target(self, receiver: str, bare: str, line: int) -> tuple[str, str] | None:
        """Rung 2b: `client.query()` where the tree types
        `client` as `ClickHouseClient` binds to the one `…::ClickHouseClient::query`
        the table holds — a parameter annotation, a typed declaration, a
        construction, an assertion or a class field behind `this.x`. The
        candidate set is rung 3's; the receiver's type narrows it. In Go, a
        type written `st.Store` narrows to the package `st` imports; a method
        the type does not define is looked up on the types it embeds
        (promotion), in this file's declaration order."""
        if self.typer is None or not receiver:
            return None
        tname = self._py_receiver_type(line, receiver) if self.python else self.typer.type_of(
            line, receiver,
        )
        if not tname:
            return None
        candidates = self._once(("typed", bare), lambda: _with_local_rows(
            self._table(bare, bare), self.local_by_name.get(bare, [])
        ))
        if not self.go:
            return self._named_type_target(tname, bare, candidates, receiver)
        alias, _, type_bare = tname.rpartition(".")
        pkg_dir = self.go_packages.get(alias, None) if alias else None
        if alias and pkg_dir == "":
            return None  # an imported library's type: nothing of the repository's
        if pkg_dir:
            pkg_dir = pkg_dir + "."  # the directory's dotted scope, then the package clause
        elif not alias and self.go_own_scope:
            pkg_dir = self.go_own_scope + "::"  # the file's own package
        elif alias and alias not in self.go_aliases:
            # The type is written under the package's CLAUSE
            # (`clickhouse.Dynamic` for …/clickhouse-go/v2, whose path element
            # says clickhouse_go): the imported repository directory whose
            # clause is the alias, or nothing — never every package's Dynamic
            clauses = {f"{d}.{alias}::" for d in self.go_packages.values() if d}
            heads = self._of(candidates, ("heads",), lambda: {
                q[1].partition("::")[0] + "::" for q in candidates if "::" in q[1]
            })
            matched = [c for c in clauses if c in heads]
            if len(matched) != 1:
                return None
            pkg_dir = matched[0]
        seen: set[tuple[str, str | None]] = set()
        queue: list[tuple[str, str | None]] = [(type_bare, pkg_dir)]
        while queue:
            t, scope = queue.pop(0)
            if (t, scope) in seen:
                continue
            seen.add((t, scope))
            narrowed = self._member_rows(candidates, t, bare)
            if scope:
                narrowed = [c for c in narrowed if c[1].startswith(scope)]
            if len(narrowed) == 1:
                return narrowed[0][0], narrowed[0][1]
            if narrowed:
                return None
            # `type Dynamic = Variant` — the alias's methods
            # are the target's; a qualified target moves to its package
            for qn, target in self.go_type_aliases.items():
                if qn.rsplit("::", 1)[-1] != t or (scope and not qn.startswith(scope)):
                    continue
                talias, _, tbare = target.rpartition(".")
                if not talias:
                    queue.append((tbare, scope))
                elif self.go_packages.get(talias):
                    queue.append((tbare, self.go_packages[talias] + "."))
                elif talias not in self.go_packages:
                    # the alias's target lives in a package THIS file does not
                    # import (`type Dynamic = chcol.Dynamic` in another file):
                    # the one repository directory whose clause is talias among
                    # the candidates' packages — then the walk continues there
                    dirs = self._clause_dirs(candidates, talias)
                    if len(dirs) == 1:
                        queue.append((tbare, next(iter(dirs)) + "."))
            # An embedded type of this package keeps the
            # scope; one written `st.Base` moves to the package `st` imports;
            # a library's (`net.Conn`, `sync.Mutex`) promotes nothing here
            for embed in self.go_embeds.get(t, []):
                ealias, _, ebare = embed.rpartition(".")
                if not ealias:
                    queue.append((ebare, scope))
                elif self.go_packages.get(ealias):
                    queue.append((ebare, self.go_packages[ealias] + "."))
        return None

    def resolve(
        self, caller: Symbol, name: str, bare: str, line: int, receiver: str = "",
    ) -> Edge | None:
        if self.python and receiver == "super()":
            return self.py_super_target(caller, bare, line)
        if (self.go or self.python) and receiver and receiver not in ("self", "cls"):
            # A Go member call on a receiver the tree types
            # is that type's method or nothing of this file — the same-file
            # namesake (`col.offsets.Rows()` beside `Map.Rows`) is never it.
            # The same for Python — `lease.acquire()` on a
            # `lease = MigrationLease(...)` is that class's method, not the
            # test double's `acquire` this file defines beside it
            typed = self.typed_target(receiver, bare, line)
            if typed is not None:
                return Edge(
                    "calls", caller.id, typed[0], caller.qualified_name,
                    typed[1], self.path, line, TYPED_RECEIVER_CONFIDENCE,
                )
            if self.go and self.typer is not None and self.typer.type_of(line, receiver):
                return self._resolve_repo_wide(caller, name, bare, line, receiver)
        # 0) `super.m()` is the bound base's `m`, never a
        # same-file namesake — the derived method itself is usually one
        if receiver == "super" and not self.go:
            return self._super_target(caller, bare, line)
        # 1) same-file unique definition — not for a Python member call on a
        # receiver the tree could not type: `lease.acquire()`
        # beside a test double's `acquire` is not the double's; the typed,
        # import and repo-wide rungs decide it
        names = self._local(bare)
        # A call written with a scope (`IStorage::method()`
        # inside `MergeTreeData::method`) names that scope's definition — the
        # same-file unique `method` (the override) and the enclosing class's
        # own `method` are not candidates unless the scope names them —
        # otherwise the override would bind as a method calling itself, where
        # the compiler tier sees the base's.
        qualifier = self._call_scope(name, bare) if not self.python else ""
        if qualifier:
            names = self._once(("scoped", bare, qualifier), lambda: _Namesakes([
                s for s in self._local(bare).syms if _in_scope(s.qualified_name, qualifier, bare)
            ]))
        n, target = names.one("", caller.qualified_name)
        if self.python and receiver and receiver not in ("self", "cls"):
            n = 0
        elif self.lang == "cpp" and receiver and receiver != "this":
            # `volume->getDisk()` is the receiver's method —
            # the file's one `getDisk` (the enclosing class's) is not it
            n = 0
        elif self.python and n:
            # A bare call inside a function names the closure
            # defined in that function (`go()` beside 788 other `go`s), then one
            # in an enclosing function; a class's method is never a bare call's
            nested = self._py_nested_def(caller, names)
            if nested is not None:
                n, target = 1, nested
            else:
                free = self._once(("free", bare), lambda: _Namesakes([
                    s for s in names.syms if not self._py_is_method(s)
                ]))
                n, target = free.one("", caller.qualified_name)
        if n == 1 and target is not None:
            return Edge(
                "calls", caller.id, target.id, caller.qualified_name,
                target.qualified_name, self.path, line, 1.0,
            )
        # 1b) `new Widget()` names a class this file defines
        if not n and len(self.local_class_by_name.get(bare, [])) == 1:
            target = self.local_class_by_name[bare][0]
            return Edge(
                "calls", caller.id, target.id, caller.qualified_name,
                target.qualified_name, self.path, line, 1.0,
            )
        # 2) method of the enclosing class — for a bare call or a call on
        # `this` / `super`; `this.raw.getHeader()` names the RECEIVER's
        # method, not the class's own (binding it would tie a prototype method
        # to itself through `this.raw`)
        if (
            "::" in caller.qualified_name and receiver in ("", "this", "super", "self", "cls")
            and not self.go  # a bare Go call is never the receiver's method (no `this`)
            # Nor is a bare Python call — a method needs
            # `self.`; `merge(...)` inside `DataFrame.join` is the imported
            # `reshape.merge.merge`, never `DataFrame.merge` itself
            and not (self.python and receiver == "")
        ):
            cls_prefix = caller.qualified_name.rsplit("::", 1)[0]
            n, target = self._local(bare).one(cls_prefix + "::", "")
            if qualifier and not _in_scope(cls_prefix + "::" + bare, qualifier, bare):
                n = 0  # `Base::m()` is never the enclosing class's `m`
            if n == 1 and target is not None:
                return Edge(
                    "calls", caller.id, target.id, caller.qualified_name,
                    target.qualified_name, self.path, line, 0.8,
                )
            if self.python and not n and receiver in ("self", "cls"):
                # `self.m()` where the class does not define
                # `m` — the method it inherits, through the heritage clauses
                inherited = self.py_inherited_target(caller, cls_prefix, bare, line)
                if inherited is not None:
                    return inherited
        # 2b) the receiver's type names the class
        typed = self.typed_target(receiver, bare, line)
        if typed is not None:
            return Edge(
                "calls", caller.id, typed[0], caller.qualified_name,
                typed[1], self.path, line, TYPED_RECEIVER_CONFIDENCE,
            )
        return self._resolve_repo_wide(caller, name, bare, line, receiver)

    def _repo_wide_pool(
        self, candidates: list[tuple[str, str, str]], bare_call: bool, member: bool,
    ) -> list[tuple[str, str, str]]:
        """Rung 3's candidates before the caller is left out: field-based for a script
        call (a member call names a property, a bare call a binding), and of the caller's
        language."""
        if self.script and member:
            candidates = self.field_based("" if bare_call else "x", candidates)
        if self.present is not None and not self.python:
            # A TypeScript, Go or shell call binds a definition of its own
            # language — the shared module-scoped name scheme also holds the other
            # script languages' definitions (Python is gated separately below)
            candidates = [c for c in candidates if self.same_language_definition(c[1])]
        if self.python and self.py_present is not None:
            # A Python call binds a Python definition — the
            # shared module-scoped name scheme also holds TypeScript's
            candidates = [c for c in candidates if self.py_definition(c[1])]
        if self.python:
            # A module constant, a class attribute or a type
            # alias of the name is not what `f()` calls
            candidates = [c for c in candidates if c[2] not in _PY_NOT_CALLABLE]
            if bare_call:
                # Nor is any class's method — a bare call has
                # no receiver to reach one through (same-file methods bind above)
                candidates = [c for c in candidates if not self._py_method_qualified(c[1])]
        return candidates

    def _repo_wide_one(
        self, pool: list[tuple[str, str, str]], skip: str,
    ) -> tuple[str, str, str] | None:
        """Rung 3's one candidate with the caller (`skip`, '' for none) left out — a
        member call on a receiver other than `this` that names only the caller is a
        library object's method, not recursion — then, of several, the one concrete Go
        method or the script implementations over their `.d.ts` twins. The pool holds
        one row per qualified name (`candidates_for_call`), so the caller is at most one
        row: each count is the pool's less that row, and no list is copied per caller."""
        n, rows = self._without(pool, skip)
        if self.go and n > 1:
            concrete = self._of(pool, ("concrete",), lambda: [
                c for c in pool if c[2] not in ("method", "field")
            ])
            k, kept = self._without(concrete, skip)
            if k == 1:
                n, rows = k, kept
        if self.script and n > 1:
            impl = self._of(pool, ("implementations",), lambda: [
                c for c in pool if not c[1].split("::", 1)[0].endswith(".d")
            ])
            k, kept = self._without(impl, skip)
            if k and k < n:
                n, rows = k, kept
        return rows[0] if n == 1 else None

    def _without(
        self, rows: list[tuple[str, str, str]], skip: str,
    ) -> tuple[int, list[tuple[str, str, str]]]:
        """How many of `rows` remain without the one named `skip`, and the first two."""
        qns = self._of(rows, ("qns",), lambda: {c[1] for c in rows})
        n = len(rows) - (1 if skip and skip in qns else 0)
        return n, list(itertools.islice((c for c in rows if not skip or c[1] != skip), 2))

    @staticmethod
    def _call_scope(name: str, bare: str) -> str:
        """The scope a call was written with (`IStorage` in
        `IStorage::getInMemoryMetadataPtr`), '' for a bare or member call."""
        if "::" not in name or not name.endswith(bare):
            return ""
        return name[: -len(bare)].rstrip(":")

    def _resolve_repo_wide(
        self, caller: Symbol, name: str, bare: str, line: int, receiver: str,
    ) -> Edge | None:
        # 2c) the file's import names the module — or, in
        # Go, the package directory the module index resolves
        candidates = self._once(("callable", name, bare), lambda: [
            c for c in self._table(name, bare) if c[2] not in _NEVER_CALLABLE
        ])
        bound = self.go_package_bound(receiver, bare, candidates)
        if bound is None and receiver == "":
            bound = self.go_same_package(caller, bare, candidates)
        if bound is None:
            bound = self.go_clause_bound(receiver, bare, candidates)
        if bound is None:
            bound = self.import_bound(receiver, bare, candidates)
        if bound is False:
            return None
        if isinstance(bound, tuple):
            return Edge(
                "calls", caller.id, bound[0], caller.qualified_name,
                bound[1], self.path, line, bound[2],
            )
        if self.python and receiver and receiver not in ("self", "cls") and self._py_library_call(
            receiver, bare, line,
        ):
            return None  # `raw.decode()`, `p.exists()`: a library type's method, not ours
        if (
            self.lang == "cpp" and receiver and receiver != "this" and self.typer is not None
            and self.typer.is_library(line, receiver)
        ):
            return None  # `parts.size()` on a std::vector is nobody's here
        # 3) unique repo-wide name: what narrows the candidates depends on the call
        # only through these, so a file of many calls narrows each name once
        member = receiver not in ("this", "super")
        pool = self._of(
            candidates, ("rung 3", receiver == "", member),
            lambda: self._repo_wide_pool(candidates, receiver == "", member),
        )
        skip = caller.qualified_name if receiver not in ("", "this", "super") else ""
        one = self._repo_wide_one(pool, skip)
        if one is not None:
            cid, cqname = one[0], one[1]
            return Edge(
                "calls", caller.id, cid, caller.qualified_name,
                cqname, self.path, line, REPO_WIDE_CONFIDENCE,
            )
        return None


def callee_targets(
    pf: ParsedFile, name_table: dict[str, list[tuple[str, str, str]]],
    lane_context: dict[str, Any] | None = None,
    callee_returns: dict[str, str] | None = None,
) -> dict[str, str]:
    """Callee as written -> the qualified name it binds to,
    for every callee some `const x = f(…)` in the file waits on — the
    same-file function, else the import-bound or unique definition from the
    name table. The caller reads those qualified names' return types
    (`CodeRepo.return_types_for_qualified`) and hands them back through
    `extract_edges_and_misses(callee_returns=…)`."""
    typed_lang = pf.lang in _EXTRA_CALL_NODES_BY_LANG or pf.lang in ("python", "go")
    if not typed_lang or pf.tree is None:
        return {}
    from server.code.domain.extract.ts_receivers import ReceiverTyper

    local_by_name: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        local_by_name.setdefault(s.name, []).append(s)
    # A second round hands the first round's return types
    # back in, so a receiver typed by one call (`conn, _ := GetConn()`)
    # resolves the callee whose return the next binding waits on
    # (`batch, _ := conn.PrepareBatch()` → `batch.Append`)
    resolver = _CallResolver(pf, name_table, local_by_name, callee_returns, lane_context)
    typed_here = {s.name for s in pf.symbols if s.kind == "function" and s.return_type}
    out: dict[str, str] = {}
    typer: Any
    if pf.lang == "python":
        from server.code.domain.extract.py_receivers import PyReceiverTyper

        typer = PyReceiverTyper(pf)
    elif pf.lang == "go":
        from server.code.domain.extract.go_receivers import GoReceiverTyper

        typer = GoReceiverTyper(pf)
    else:
        typer = ReceiverTyper(pf)
    if pf.lang == "python":
        out.update(_fixture_targets(pf, name_table))
    for callee in typer.callees():
        if callee.startswith("fixture:"):
            continue
        if callee.startswith("name:"):
            _bind_name_receiver(resolver, name_table, callee, out)
            continue
        bare = callee.rsplit(".", 1)[-1]
        receiver = callee.rsplit(".", 1)[0] if "." in callee else ""
        if bare in typed_here:
            continue  # the same file answers without a read
        candidates = resolver._table(bare, bare)
        bound = resolver.go_package_bound(receiver, bare, candidates)
        if bound is None and receiver == "" and resolver.go and pf.symbols:
            # A bare `Conn(t)` is this package's helper first
            bound = resolver.go_same_package(pf.symbols[0], bare, candidates)
        if bound is None:
            bound = resolver.go_clause_bound(receiver, bare, candidates)
        if bound is None:
            bound = resolver.import_bound(receiver, bare, candidates)
        if bound is False:
            continue
        if isinstance(bound, tuple):
            out[callee] = bound[1]
            continue
        typed = resolver.typed_target(receiver, bare, 0) if receiver else None
        if typed is not None:
            out[callee] = typed[1]
            continue
        narrowed = resolver.field_based(receiver, candidates) if receiver != "this" else candidates
        if len({c[1] for c in narrowed}) == 1:
            out[callee] = narrowed[0][1]
    return out


def _bind_name_receiver(
    resolver: _CallResolver, name_table: dict[str, list[tuple[str, str, str]]],
    callee: str, out: dict[str, str],
) -> None:
    """`name:<receiver>` — an imported name used as a receiver
    binds to its definition through the import alone (never a repo-wide
    guess); the caller reads that definition's annotation as the type."""
    name = callee[len("name:"):]
    bound = resolver.import_bound("", name, resolver._table(name, name))
    if isinstance(bound, tuple):
        out[callee] = bound[1]


def _fixture_targets(
    pf: ParsedFile, name_table: dict[str, list[tuple[str, str, str]]],
) -> dict[str, str]:
    """`fixture:<name>` → the fixture definition's qualified
    name, by pytest's nearest-first ladder (`_resolve_fixtures`), so a test's
    untyped parameter is typed by the fixture's yield annotation."""
    if not pf.fixture_requests:
        return {}
    local_syms = {s.qualified_name: s for s in pf.symbols}
    out: dict[str, str] = {}
    for e in _resolve_fixtures(pf, name_table, local_syms):
        name = e.to_qualified_name.rsplit("::", 1)[-1]
        out.setdefault(f"fixture:{name}", e.to_qualified_name)
    return out


def _include_edges(pf: ParsedFile) -> list[Edge]:
    """includes: file -> file pseudo-symbols, one edge per include."""
    from_file = file_symbol_id(pf.path)
    return [
        Edge(
            edge_kind="includes", from_symbol_id=from_file, to_symbol_id=file_symbol_id(inc),
            from_qualified_name=pf.path, to_qualified_name=inc, path=pf.path, line=0,
            confidence=1.0,
        )
        for inc in pf.includes
    ]


def extract_edges_and_misses(
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
    header: HeaderDecls | None = None,
    callee_returns: dict[str, str] | None = None,
    lane_context: dict[str, Any] | None = None,
    chains: dict[str, tuple[str, str]] | None = None,
) -> tuple[list[Edge], list[CallMiss]]:
    """Edges plus the call sites that produced none. Noise names (`size`,
    `print`) are neither — they are not calls anyone asks about. `header`: the
    declarations the ingest read from the headers this file reaches; None binds
    without any header declarations."""
    misses: list[CallMiss] = []
    edges: list[Edge] = _include_edges(pf)
    source = pf.source

    # A googletest body (kind `test`) is a caller — its calls
    # are what code.tests_for reads — but never a target
    fn_syms = [s for s in pf.symbols if s.kind in ("function", "test") and s.has_body]
    # Heritage binds through the import rung too, and a file
    # with no function bodies (an error subclass) still declares its bases
    heritage, bases = _heritage_and_overrides(pf, name_table, chains)
    edges.extend(heritage)
    if pf.lang == "go":
        # Interface satisfaction is structural — derived from
        # the method sets the name table holds, in both directions
        from server.code.domain.extract.go_implementors import implementor_edges

        # The implementing types' own rows join the table
        impl_table = dict(name_table)
        for bare, rows in (lane_context or {}).get("go_type_rows", {}).items():
            impl_table[bare] = [*impl_table.get(bare, []), *rows]
        edges.extend(implementor_edges(
            pf, impl_table, Edge, (lane_context or {}).get("go_method_sigs"),
        ))
        # A handler that embeds `Unimplemented<Svc>Server` or
        # is registered for the service implements the proto lane's rows
        from server.code.domain.extract.go_directives import embed_edges, go_directives
        from server.code.domain.extract.go_grpc import grpc_edges

        grpc_table = dict(name_table)
        grpc_table.update((lane_context or {}).get("go_grpc_table", {}))
        edges.extend(grpc_edges(
            pf, grpc_table, Edge, (lane_context or {}).get("go_grpc_external"),
        ))
        # `//go:embed` — the variable reads the embedded files
        edges.extend(embed_edges(
            pf, go_directives(pf), list((lane_context or {}).get("go_embed_paths", [])), Edge,
        ))

    # pytest fixture wiring (python only; empty list elsewhere)
    edges.extend(
        _resolve_fixtures(pf, name_table, {s.qualified_name: s for s in pf.symbols})
    )

    call_node = _CALL_NODE_BY_LANG.get(pf.lang)
    if pf.tree is None or call_node is None or (not fn_syms and file_unit(pf) is None):
        return edges, misses  # a script file's top level is a unit of its own

    # same-file resolution index: name -> [Symbol]; unique names resolve at 1.0
    local_by_name: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        if s.kind == "function":
            local_by_name.setdefault(s.name, []).append(s)

    # the enclosing function of each call site by line interval: innermost wins because
    # spans sort widest-first per start
    enclosing = last_span_at(_caller_spans(pf, fn_syms))

    noise = _NOISE_BY_LANG.get(pf.lang, _NOISE_CPP)
    fallback = _fallback_caller(pf)  # the class, else the file

    cpp = pf.lang == "cpp"
    local_classes = {s.name for s in pf.symbols if s.kind in {"class", "struct"}}
    table_kinds: dict[str, set[str]] = {}  # the kinds the table holds per name, read once

    def _table_kinds(bare: str) -> set[str]:
        if bare not in table_kinds:
            table_kinds[bare] = {c[2] for c in name_table.get(bare, [])}
        return table_kinds[bare]

    def _names_a_class(bare: str) -> bool:
        """Is `bare` a class the index knows (same file or repo-wide)? Then
        `Widget(5)` is a construction, not a call to a function named Widget."""
        if bare in local_classes:
            return True
        return not _table_kinds(bare).isdisjoint({"class", "struct"})

    def _ctor_site(line: int, written: str, argc: int, declared: int = 0) -> bool:
        """Record the construction as an edge or an ambiguous miss; True when
        either was recorded (a type with no indexed constructor is neither)."""
        caller = enclosing(line)
        if caller is None:
            return False
        bare = written.split("::")[-1]
        edge, miss = _resolve_ctor(caller, written, bare, argc, line, declared)
        if edge is not None:
            edges.append(edge)
            return True
        if miss is not None:
            misses.append(miss)
            return True
        return False

    def walk_calls(node: Any) -> None:
        for child in node.named_children:
            if _is_call(child, call_node, pf.lang):
                line = child.start_point[0] + 1
                # Additive by design: constructions are recorded ON TOP of
                # the ordinary call binding. A factory call is both a
                # call to the factory (`DB::make_intrusive` is a real function
                # with real callers) and a construction of its type argument.
                factory = _ctor_of_call(child, source) if cpp else None
                if factory is not None:
                    _ctor_site(line, factory[0], factory[1])
                name = _call_name(child, source)
                bare = name.split("::")[-1].split(".")[-1]
                if child.type == "type_conversion_expression" and "function" not in _table_kinds(
                    bare
                ):
                    walk_calls(child)
                    continue  # a conversion to a generic type, not a call
                caller = enclosing(line) or fallback(line)
                receiver = _receiver_text(child, source)
                # `client.query()` on a receiver the tree types
                # is a call somebody asks about, whatever `query` is elsewhere
                if caller is not None and name and not _declined(receiver, line) and (
                    bare not in noise or _typed_receiver_target(receiver, bare, line) is not None
                    or receiver in _MODULE_SELF  # exports.f() is this module's f
                    or _imported_noise(resolver, receiver, bare, name_table)
                    # `cht.New()` on a repository package is that package's New
                    or (resolver.go and receiver in resolver.go_aliases
                        and bool(resolver.go_packages.get(receiver)))
                ):
                    edge = _resolve_call(caller, name, bare, line, receiver)
                    if edge is not None:
                        edges.append(edge)
                    elif not (
                        # `Widget(5)` / `DB::Widget(5)`, the temporary form: a
                        # name the ladder could not bind as a function but
                        # that names a class is a construction
                        cpp and factory is None and "." not in name and _names_a_class(bare)
                        and _ctor_site(line, name, _argc(child.child_by_field_name("arguments")))
                    ):
                        misses.append(_miss_of(caller, name, bare, line))
            elif cpp and child.type in _CTOR_NODE_TYPES:
                for line, written, argc in _ctor_sites_of_node(child, source):
                    _ctor_site(line, written, argc)
            walk_calls(child)

    def _resolve_ctor(
        caller: Symbol, written: str, bare: str, argc: int, line: int, declared: int = 0,
    ) -> tuple[Edge | None, CallMiss | None]:
        return resolve_ctor(
            caller, written, argc, line, path=pf.path,
            local=local_by_name.get(bare, []), name_table=name_table, declared=declared,
        )

    def _miss_of(caller: Symbol, name: str, bare: str, line: int) -> CallMiss:
        return resolver.miss(caller, name, bare, line)

    resolver = _CallResolver(pf, name_table, local_by_name, callee_returns, lane_context, chains)
    if header is not None and pf.lang == "cpp" and resolver.typer is not None:
        resolver.typer.bind_header(header)  # the headers' fields type receivers
    resolver.bases = bases
    _resolve_call = resolver.resolve
    _typed_receiver_target = resolver.typed_target
    _declined = resolver.declined

    walk_calls(pf.tree.root_node)
    if cpp:
        # the remainder: member-init lists and emplace calls,
        # bound through the declarations this file holds; declined sites are
        # neither edge nor miss — the callee is a constructor of a type the
        # file does not name, so there is no name to record a miss under
        # (and `emplace_back` is noise: a miss under it would feed closure
        # repair with a container method)
        for site in remainder_ctor_sites(pf, header):
            if site.written and not site.decline:
                # a header-typed site: the header's overload count guards the
                # repo-wide rung
                declared = header.declared_ctors(site.written) if header is not None else 0
                _ctor_site(site.line, site.written, site.argc, declared)
        edges.extend(_registration_edges(pf, name_table, header, enclosing))
    return _dedupe(edges, misses)


def _registration_edges(
    pf: ParsedFile, name_table: dict[str, list[tuple[str, str, str]]],
    header: HeaderDecls | None, enclosing: Any,
) -> list[Edge]:
    """Registration edges: the registration statement is the
    caller of the class it hands to the registry — its methods in this file,
    its `create` / `executeImpl` repo-wide, the creator function passed by
    name. `calls` rows; the read side derives `via: registration` from the
    caller's shape."""
    from server.code.domain.extract import registration

    return [
        Edge(
            "calls", r.caller.id, r.to_symbol_id, r.caller.qualified_name,
            r.to_qualified_name, pf.path, r.line, r.confidence,
        )
        for r in registration.registration_edges(
            pf, name_table, enclosing=enclosing, aliases=_file_aliases(pf, header),
        )
    ]


def _dedupe(edges: list[Edge], misses: list[CallMiss]) -> tuple[list[Edge], list[CallMiss]]:
    """Identical (kind, from, to, path, line) edges and (callee, path, line,
    from) misses collapse — macro expansion repeats a site."""
    seen: set[tuple[str, str, str, str, int]] = set()
    out: list[Edge] = []
    for e in edges:
        k = (e.edge_kind, e.from_symbol_id, e.to_symbol_id, e.path, e.line)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    seen_m: set[tuple[str, str, int, str]] = set()
    out_m: list[CallMiss] = []
    for m in misses:
        km = (m.callee_name, m.path, m.line, m.from_symbol_id)
        if km in seen_m:
            continue
        seen_m.add(km)
        out_m.append(m)
    return out, out_m
