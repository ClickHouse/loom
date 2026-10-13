"""TypeScript / JavaScript syntax-tree helpers the parser walks with: the grammar per
extension, function-like and wrapped nodes, signatures and return types, doc comments
and slugs."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from server.code.domain.extract.treesitter import node_text

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
_MAX_SLUG = 60

_TS_GRAMMAR_BY_EXT: dict[str, str] = {
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
}


_MAX_DOC = 2000


_FUNCTION_LIKE: frozenset[str] = frozenset(
    {
        "arrow_function",
        "function_expression",
        "function",
        "generator_function",
    }
)
_WRAPPERS: frozenset[str] = frozenset(
    {
        "as_expression",
        "satisfies_expression",
        "parenthesized_expression",
        "non_null_expression",
        "type_assertion",
        "await_expression",
    }
)


def _grammar_for(path: str) -> str:
    lower = path.lower()
    ext = lower[lower.rfind(".") :] if "." in lower else ""
    return _TS_GRAMMAR_BY_EXT.get(ext, "javascript")


@lru_cache(maxsize=3)
def _parser(grammar: str) -> Any:
    import tree_sitter

    if grammar == "javascript":
        import tree_sitter_javascript

        return tree_sitter.Parser(tree_sitter.Language(tree_sitter_javascript.language()))
    import tree_sitter_typescript

    lang = (
        tree_sitter_typescript.language_tsx()
        if grammar == "tsx"
        else tree_sitter_typescript.language_typescript()
    )
    return tree_sitter.Parser(tree_sitter.Language(lang))


def _unwrap(node: Any) -> Any:
    """`(x)`, `x as T`, `x satisfies T`, `x!` → x."""
    while node is not None and node.type in _WRAPPERS:
        inner = next(
            (
                c
                for c in node.named_children
                if c.type
                not in {
                    "type_identifier",
                    "generic_type",
                    "predefined_type",
                    "object_type",
                    "union_type",
                }
            ),
            None,
        )
        if inner is None:
            break
        node = inner
    return node


def _wrapped_function(node: Any, depth: int = 0) -> Any:
    """The function a call wraps — `forwardRef<P>((props, ref) => …)`, `memo(function
    X() {})`, `observer(memo(fn))`, `styled(Base)(fn)` — or None. A component declared
    through a higher-order wrapper is a function unit, not a const (the JSX render sites
    bind to it and the calls inside it attribute to it)."""
    node = _unwrap(node)
    if node is None or node.type != "call_expression" or depth > 2:
        return None
    args = node.child_by_field_name("arguments")
    for arg in args.named_children if args is not None else []:
        arg = _unwrap(arg)
        if arg is None:
            continue
        if _is_function_like(arg):
            return arg
        inner = _wrapped_function(arg, depth + 1)
        if inner is not None:
            return inner
    return None


def _is_function_like(node: Any) -> bool:
    return node is not None and _unwrap(node).type in _FUNCTION_LIKE


def bound_function(value: Any) -> Any:
    """The function a variable's value is, as the index reads one: through `(…)`, `as`,
    `satisfies`, `!` and wrapper calls; None when it is not one."""
    return _unwrap(value) if _is_function_like(value) else _wrapped_function(value)


def _chain_function(call: Any) -> Any:
    """The function-like argument of a call chain (`a.b(x).c(fn)` → fn),
    the outermost call's first: None when no call in the chain takes one."""
    node = call
    while node is not None and node.type == "call_expression":
        args = node.child_by_field_name("arguments")
        for arg in args.named_children if args is not None else []:
            if _is_function_like(arg):
                return _unwrap(arg)
        fn = node.child_by_field_name("function")
        member = fn is not None and fn.type == "member_expression"
        node = fn.child_by_field_name("object") if member else None
    return None


def _signature(fn: Any, source: bytes) -> str:
    """Parameters plus the return-type annotation, as written."""
    params = fn.child_by_field_name("parameters")
    ret = fn.child_by_field_name("return_type")
    sig = node_text(params, source) if params is not None else ""
    if ret is not None:
        sig += node_text(ret, source)
    return sig


_JSDOC_RETURNS_RE = re.compile(r"@returns?\s*\{([^}]+)\}")


def _return_type_of(fn: Any, source: bytes, doc: str = "") -> str:
    """The one named type a function returns — its annotation (`(): Reply`,
    `Promise<Reply>` → Reply: the awaited value is what a caller holds), else a JSDoc
    `@returns {Reply}`, else the class every `return new T(…)` in its body constructs
    (one class, or '')."""
    from server.code.domain.extract.ts_receivers import type_name_of

    fn = _unwrap(fn)
    if fn is None:
        return ""
    ann = fn.child_by_field_name("return_type")
    if ann is not None:
        name = type_name_of(ann, source)
        if not name:
            # `Promise<T>`: the awaited T is what the caller binds
            inner = _promise_argument(ann, source)
            name = type_name_of(inner, source) if inner is not None else ""
        if name and name != "Promise":
            return name
        if name == "Promise":
            inner = _promise_argument(ann, source)
            return type_name_of(inner, source) if inner is not None else ""
    m = _JSDOC_RETURNS_RE.search(doc or "")
    if m:
        text = re.sub(r"^Promise<(.+)>$", r"\1", m.group(1).strip())
        return text if re.fullmatch(r"[A-Za-z_$][\w$]*", text) else ""
    body = fn.child_by_field_name("body")
    if body is None:
        return ""
    constructed: set[str] = set()
    stack = list(body.named_children)
    while stack:
        n = stack.pop()
        if n.type in _FUNCTION_LIKE or n.type in {"class_declaration", "class"}:
            continue
        if n.type == "return_statement":
            value = _unwrap(n.named_children[0]) if n.named_children else None
            if value is not None and value.type == "new_expression":
                ctor = value.child_by_field_name("constructor")
                if ctor is not None and ctor.type == "identifier":
                    constructed.add(node_text(ctor, source))
                else:
                    return ""
            else:
                return ""  # a return that is not a construction: the tree does not say
        stack.extend(n.named_children)
    return constructed.pop() if len(constructed) == 1 else ""


def _promise_argument(ann: Any, source: bytes) -> Any:
    """The `T` of a `Promise<T>` return annotation, or None."""
    node = ann.named_children[0] if ann.type == "type_annotation" and ann.named_children else ann
    if node is None or node.type != "generic_type":
        return None
    name = node.child_by_field_name("name")
    if name is None or node_text(name, source) != "Promise":
        return None
    args = node.child_by_field_name("type_arguments")
    return args.named_children[0] if args is not None and args.named_children else None


def _comment_above(node: Any, source: bytes) -> str:
    """The JSDoc block or `//` run whose last line sits right above `node`."""
    parts: list[str] = []
    sib = node.prev_named_sibling
    expected_end = node.start_point[0]
    while sib is not None and sib.type == "comment":
        if expected_end - sib.end_point[0] > 2:
            break
        parts.append(node_text(sib, source))
        if parts[-1].startswith("/*"):
            break  # one block comment is the doc; nothing above it joins
        expected_end = sib.start_point[0]
        sib = sib.prev_named_sibling
    return "\n".join(reversed(parts))[:_MAX_DOC] if parts else ""


def _statements_under(body: Any) -> list[Any]:
    """The expression statements inside a block, descending through
    blocks and control flow but never into another function — the
    statements a nested test block can sit in."""
    out: list[Any] = []
    stack = list(body.named_children)
    while stack:
        n = stack.pop()
        if n.type == "expression_statement":
            out.append(n)
        elif n.type in _FUNCTION_LIKE or n.type in {"class_declaration", "class"}:
            continue
        else:
            stack.extend(n.named_children)
    out.sort(key=lambda n: n.start_byte)
    return out


def _callee_root(call: Any, source: bytes) -> tuple[str, str]:
    """(root identifier, callee text) of a call chain: `it.each([…])("d", fn)`
    → ("it", "it.each"), `vi.mock("x", fn)` → ("vi", "vi.mock")."""
    fn = call.child_by_field_name("function")
    while fn is not None and fn.type == "call_expression":
        fn = fn.child_by_field_name("function")
    if fn is None:
        return "", ""
    text = node_text(fn, source)
    root = text.split(".", 1)[0].split("(", 1)[0]
    callee = re.sub(r"\(.*$", "", text, flags=re.S)
    return root, callee


def _first_string_arg(call: Any, source: bytes) -> str:
    """The first string / template literal argument of the outermost call."""
    args = call.child_by_field_name("arguments")
    for arg in args.named_children if args is not None else []:
        if arg.type in {"string", "template_string"}:
            return node_text(arg, source).strip("\"'`")[:200]
    return ""


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text).strip("_")[:_MAX_SLUG]


def _key_text(key: Any, source: bytes) -> str:
    if key is None:
        return ""
    if key.type in {"string", "string_fragment"}:
        return node_text(key, source).strip("\"'`")
    if key.type == "computed_property_name":
        return node_text(key, source).strip("[]").strip("\"'`")
    return node_text(key, source)
