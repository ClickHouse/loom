"""A definition's declaration as its language parses it: where it starts once its
decorators, attributes and `template <...>` head are counted, and its rename shape (its
tokens with the declared name and the bare references to it blanked). A language with
no grammar here has neither: its text starts where the index says and it is never
matched as a rename."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterator, Sequence
from functools import lru_cache
from typing import Any, NamedTuple

from server.code.domain.anchor import NO_SHAPE, Shape, fingerprint, unnumbered
from server.code.domain.extract.lang_typescript_nodes import (
    _is_function_like,
    _unwrap,
    _wrapped_function,
)

_GRAMMARS = {
    ".py": "python",
    ".pyi": "python",
    ".c": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".ipp": "cpp",
    ".inl": "cpp",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
}
# a node that holds the declaration with its decorators or template head
_HOLDERS = {
    "decorated_definition",
    "template_declaration",
    "export_statement",
    "ambient_declaration",
}
# a body holding declarations: a Python block starts at its first statement, so its
# only member starts and ends with it
_SCOPES = {"block", "module"}
# decorators and attributes written as siblings before the declaration
_PREFIXES = {"decorator", "attribute_item"}
_COMMENTS = {"comment", "line_comment", "block_comment"}  # Rust's are the last two
# `const NAME = () => …`: a function bound to a variable, as the index declares it
_VARIABLES = {"lexical_declaration", "variable_declaration"}
# the nodes that declare the name they carry (a call or a qualified reference has a
# `name` field too, and declares nothing)
_DECLARATIONS = {
    # Python, C and C++
    "function_definition",
    "class_definition",
    "class_specifier",
    "struct_specifier",
    "union_specifier",
    "enum_specifier",
    "namespace_definition",
    "field_declaration",
    "type_definition",
    "alias_declaration",
    # JavaScript and TypeScript
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "class_declaration",
    "abstract_class_declaration",
    "class",
    "method_definition",
    "method_signature",
    "abstract_method_signature",
    "function_signature",
    "public_field_definition",
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
    "variable_declarator",
    # Go
    "method_declaration",
    "method_elem",
    "const_spec",
    "var_spec",
    "type_spec",
    "type_alias",
    "method_spec",
    # Java
    "constructor_declaration",
    "record_declaration",
    "annotation_type_declaration",
    # Rust
    "function_item",
    "function_signature_item",
    "struct_item",
    "enum_item",
    "union_item",
    "trait_item",
    "type_item",
    "const_item",
    "static_item",
    "mod_item",
    "macro_definition",
}
# a name written as more than one token: `~Reader`, `operator()`
_COMPOSITE_NAMES = {"destructor_name", "operator_name"}
_FIELDLESS_DECLARATORS = {"reference_declarator"}
_QUALIFIED_NAMES = {"qualified_identifier", "scoped_identifier", "nested_name_specifier"}
_PREPROC_IFS = {"preproc_if", "preproc_ifdef"}
_PREPROC_BRANCHES = {"preproc_elif", "preproc_elifdef", "preproc_else"}
# what selects a branch: an `#if` or `if` condition; a later branch negates those before it
_CONDITIONALS = _PREPROC_IFS | {"preproc_elif", "preproc_elifdef", "if_statement", "elif_clause"}
_ALTERNATIVES = _PREPROC_BRANCHES | {"elif_clause", "else_clause"}
_HANDLERS = {"except_clause", "except_group_clause"}
# what may stand before a declaration's own first token
_HEADS = _PREFIXES | {
    "marker_annotation",
    "annotation",
    "attribute_declaration",
    "attribute_specifier",
    "ms_declspec_modifier",
    "alignas_qualifier",
    "template",
    "template_parameter_list",
    "requires_clause",
}
_ACCESS = {".", "::", "->", "?."}
# the nodes an import declares names in
_IMPORTS = {
    "import_statement",
    "import_from_statement",
    "future_import_statement",
    "import_declaration",
    "import_spec",
    "use_declaration",
    "using_declaration",
}
_CALLS = {"call", "call_expression"}
# a node whose identifier children are only ever read
_READS = {
    "argument_list",
    "arguments",
    "return_statement",
    "return_expression",
    "expression_statement",
    "parenthesized_expression",
    "binary_operator",
    "binary_expression",
    "comparison_operator",
    "boolean_operator",
    "unary_operator",
    "unary_expression",
    "not_operator",
    "conditional_expression",
    "ternary_expression",
    "await",
    "await_expression",
}
# (node, field): an identifier in that field is read, not bound
_READ_FIELDS = {
    ("assignment", "right"),
    ("augmented_assignment", "right"),
    ("assignment_expression", "right"),
    ("variable_declarator", "value"),
    ("init_declarator", "value"),
    ("let_declaration", "value"),
    ("keyword_argument", "value"),
    ("pair", "value"),
    ("attribute", "object"),
    ("member_expression", "object"),
    ("field_expression", "argument"),
    ("field_expression", "value"),
    ("selector_expression", "operand"),
    ("subscript", "value"),
    ("subscript", "subscript"),
    ("subscript_expression", "object"),
    ("subscript_expression", "index"),
    ("index_expression", "operand"),
    ("index_expression", "index"),
    # a nested function's default, read when that function is made
    ("default_parameter", "value"),
    ("typed_default_parameter", "value"),
    ("assignment_pattern", "right"),
    ("required_parameter", "value"),
    ("optional_parameter", "value"),
    ("optional_parameter_declaration", "default_value"),
}
# a class declared inside the declaration: its members (and what it inherits) can take
# the name
_NESTED_CLASSES = {
    "class_definition",
    "class_declaration",
    "class_body",
    "class_specifier",
    "enum_body",
    "interface_body",
    "struct_specifier",
    "union_specifier",
    "impl_item",
    "object_creation_expression",
    "class",
}
# the nodes a method's name is declared in: a bare call there is not the method
_CLASS_NODES = {"class_definition", "class_body", "class_declaration", "impl_item", "trait_item"}
# the declarations of a class, whose body names the class itself: a type, a constructor
_CLASSES = {"class_definition", "class_declaration", "class_specifier", "struct_specifier"}
_FUNCTIONS = {
    "function_definition",
    "function_declaration",
    "function_expression",
    "function_item",
    "method_definition",
    "method_declaration",
    "arrow_function",
    "func_literal",
    "lambda",
    "lambda_expression",
    "closure_expression",
}
# an import of one name that may carry an alias: the field each part is in
_ALIASED = {"aliased_import": "alias", "import_specifier": "alias", "use_as_clause": "alias"}
_PATHS = {"scoped_identifier", "qualified_identifier", "scoped_type_identifier", "dotted_name"}


def bound_function(value: Any) -> Any:
    """The function a variable's value is, as the index reads one: through `(…)`, `as`,
    `satisfies`, `!` and wrapper calls; None when it is not one. Kept out of the
    extractor modules so it moves no extractor stamp."""
    return _unwrap(value) if _is_function_like(value) else _wrapped_function(value)


def joined(shapes: Sequence[Shape]) -> Shape:
    """One shape over several declarations of a name, each part '' when any of theirs is."""
    return Shape(*(fingerprint(part) if all(part) else "" for part in zip(*shapes, strict=True)))


class Prints(NamedTuple):
    """A definition's fingerprints, and the text the judge reads of it."""

    body: str
    shape: Shape
    shown: str


class _Member(NamedTuple):
    """One declaration of a family: its text, what its fingerprint covers, its rename
    shapes, and the text the judge reads; the first two under its conditions."""

    text: str
    printed: str
    shape: Shape
    shown: str


# Bump when fingerprints or shapes change for the same code: links hashed under another
# `fingerprint_version` are re-hashed.
_RULES = 6


# the words that open a scope, its name after them (`class DLL_EXPORT Foo`, a template
# head): a class, a namespace or module, Rust's impl, Go's type
_SCOPE_WORDS = "class|struct|union|interface|trait|enum|record|object|namespace|module|type|impl"
# the words that bind a scope's name right after them, by grammar: an enclosing function,
# a Rust mod, a TS object or class bound to a name
_BINDING_WORDS = {
    "python": "def",
    "rust": "fn|mod",
    **dict.fromkeys(("typescript", "tsx", "javascript"), "function|const|let|var"),
}


def _grammar(path: str) -> str | None:
    """The tree-sitter grammar of `path` by its extension; None for one without."""
    file = path.rsplit("/", 1)[-1]
    return _GRAMMARS.get(file[file.rfind(".") :].lower()) if "." in file else None


def edit_pattern(name: str, path: str) -> str | None:
    """The `git log -G` pattern of the lines of `path` that can define the dotted
    `name`: its own part anywhere, a scope part only where it is declared (a C++ class
    name is on every out-of-line method); None when a part is no plain identifier."""
    *scopes, own = name.split(".")
    # ASCII only: [:alnum:] around other letters depends on git's locale
    if not all(re.fullmatch(r"\w+", p, re.ASCII) for p in [*scopes, own]):
        return None
    # POSIX ERE, as every git builds it: no \b or \s, word edges spelled out
    edge, end = "(^|[^[:alnum:]_])", "([^[:alnum:]_]|$)"
    pattern = f"{edge}{own}{end}"
    if scopes:
        named = "|".join(scopes)
        # anything up to the name but a body: `__attribute__((aligned(8)))` too; never
        # right after `(`, so a first Python base class is none (a later one over-matches,
        # costing a candidate commit)
        between = "[^{]*"
        pattern += (
            f"|{edge}({_SCOPE_WORDS})[^[:alnum:]_{{(]({between}[^[:alnum:]_{{(])?({named}){end}"
        )
        if binding := _BINDING_WORDS.get(_grammar(path) or ""):
            pattern += f"|{edge}({binding})[[:space:]]+({named}){end}"
    return pattern


@lru_cache(maxsize=1)
def fingerprint_version() -> str:
    """The rules and the tree-sitter packages a fingerprint is computed with, as a short
    tag: a grammar upgraded, or missing (texts then fall back to lines), changes it."""
    from importlib.metadata import PackageNotFoundError, version

    grammars = {"typescript" if g == "tsx" else g for g in _GRAMMARS.values()}
    installed = []
    for package in ["tree-sitter", *sorted(f"tree-sitter-{g}" for g in grammars)]:
        try:
            installed.append(f"{package}={version(package)}")
        except PackageNotFoundError:
            installed.append(f"{package}=-")
    digest = hashlib.sha256(",".join(installed).encode()).hexdigest()[:12]
    return f"{_RULES}-{digest}"


@lru_cache(maxsize=8)
def _parser(grammar: str) -> Any:
    import importlib

    import tree_sitter

    if grammar in ("typescript", "tsx"):
        ts = importlib.import_module("tree_sitter_typescript")
        lang = ts.language_tsx() if grammar == "tsx" else ts.language_typescript()
    else:
        lang = importlib.import_module(f"tree_sitter_{grammar}").language()
    return tree_sitter.Parser(tree_sitter.Language(lang))


class Declarations:
    """The declarations of one file's source."""

    def __init__(self, source: bytes, root: Any, grammar: str = "") -> None:
        self.source, self.root, self.grammar = source, root, grammar
        self.lines = source.split(b"\n")
        self._rows: dict[int, dict[str, list[tuple[Any, Any]]]] = {}
        self._sole: dict[tuple[int, str], Any] = {}
        self._scopes: dict[tuple[int, int, str], dict[str, list[Any]]] = {}
        self._families: dict[tuple[Any, ...], list[_Member]] = {}
        self._prints: dict[tuple[tuple[Any, ...], ...], Prints] = {}

    @classmethod
    def parse(cls, path: str, source: bytes, tree: Any = None) -> Declarations | None:
        """`source`'s declarations; `tree`, the index's parse of the same bytes, is reused
        when it was made with this grammar."""
        grammar = _grammar(path)
        if grammar is None:
            return None
        try:
            parser = _parser(grammar)
            if tree is None or getattr(tree, "language", None) != parser.language:
                tree = parser.parse(source)
            return cls(source, tree.root_node, grammar)
        except Exception:  # noqa: BLE001 — no grammar, no declaration: the index's span stands
            return None

    def _at(self, line: int, name: str | None = None) -> tuple[Any, Any] | None:
        """(first node, declaration node) of the definition starting at `line`, with its
        decorators, attributes and template head; with `name`, the one there declaring
        it. None when none or several do."""
        if not 0 < line <= len(self.lines):
            return None
        text = self.lines[line - 1]
        col = len(text) - len(text.lstrip())
        node = self.root.descendant_for_point_range((line - 1, col), (line - 1, col))
        while (parent := node.parent) is not None and parent.parent is not None:
            if parent.start_byte != node.start_byte or parent.type in _SCOPES:
                break
            node = parent  # the declaration that starts at this token
        if name is not None:
            if (key := (line, name)) not in self._sole:
                self._sole[key] = self._sole_on(line, name, node)
            if (node := self._sole[key]) is None:
                return None
        if node.type == "variable_declarator" and node.parent.type in _VARIABLES:
            node = node.parent  # `const a = …,\n  b = …`: b's keyword and export are its own
        while (parent := node.parent) is not None and parent.parent is not None:
            # a parent that is the declaration with what comes before it, not a scope
            # holding more after it
            if parent.end_byte != node.end_byte or parent.type in _SCOPES:
                break
            before = [c for c in parent.children if c.end_byte <= node.start_byte]
            wraps = parent.type in _HOLDERS or all(
                c.type in _PREFIXES | _COMMENTS or c.start_point[0] == line - 1 for c in before
            )
            if not wraps:
                break
            node = parent
        return _prefixed(node)

    def _sole_on(self, line: int, name: str, node: Any) -> Any:
        """The one declaration on `line` naming `name` (`node` when it is that one), or
        None for none or several; one nested in another counts once
        (`const A = memo(function A() {…})`)."""
        first = self._declared(node, name)
        by_token = {_key(t): d for d, t in self._named_on(line - 1).get(name, [])}
        if first is not None:
            by_token[_key(first[1])] = node
        outer, end = [], -1
        for d in sorted(by_token.values(), key=lambda d: (d.start_byte, -d.end_byte)):
            if d.end_byte > end:  # not inside one already kept
                outer.append(d)
                end = d.end_byte
        if len(outer) != 1:
            return None
        return node if first is not None else outer[0]

    def _declared(self, node: Any, name: str) -> tuple[Any, Any, Any] | None:
        """(declaration, its name node, its body) when `node` declares `name`."""
        decl = _inner(node)
        if decl.type in _VARIABLES:
            return self._bound(decl, name)
        named = _name_node(decl)
        if named is None or self._text(named) != name:
            return None
        return decl, named, decl.child_by_field_name("body")

    def _named_on(self, row: int) -> dict[str, list[tuple[Any, Any]]]:
        """name -> (declaration, its name token) for each declaration whose name token is
        on `row` (0-based): one pass down the row's nodes, read once per row."""
        if (known := self._rows.get(row)) is not None:
            return known
        out: dict[str, list[tuple[Any, Any]]] = defaultdict(list)
        # from the row's own text: from column 0 an indented row is its whole class body
        text = self.lines[row]
        first = len(text) - len(text.lstrip())
        last = max(first, len(text.rstrip()))
        stack = [self.root.descendant_for_point_range((row, first), (row, last))]
        while stack:
            n = stack.pop()
            if n.start_point[0] > row or n.end_point[0] < row:
                continue
            if n.type in _DECLARATIONS and (named := _name_node(n)) is not None:
                if named.start_point[0] == row:
                    out[self._text(named)].append((n, named))
            stack.extend(n.children)
        self._rows[row] = out
        return out

    def _bound(self, decl: Any, name: str) -> tuple[Any, Any, Any] | None:
        """(declarator, name, body) of the declarator binding `name`: to a function as the
        index reads one (through `(…)`, `as`, `satisfies`, `!` and wrapper calls), else
        to a value, with no body."""
        value = None
        for d in decl.named_children:
            n, v = d.child_by_field_name("name"), d.child_by_field_name("value")
            if d.type != "variable_declarator" or n is None or self._text(n) != name:
                continue
            fn = None if v is None else bound_function(v)
            if fn is not None:
                return d, n, fn.child_by_field_name("body")
            value = value or (d, n, None)
        return value

    def _text(self, node: Any) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def start(self, line: int) -> int:
        """The line the definition at `line` starts on, its decorators counted."""
        at = self._at(line)
        return at[0].start_point[0] + 1 if at else line

    def printed(self, line: int, name: str) -> str | None:
        """What the fingerprint of the declaration at `line` covers (`_printed`); None as
        `text`."""
        name = unnumbered(name)
        at = self._at(line, name)
        return None if at is None else self._member(at, name, False).printed

    def text(self, line: int, name: str) -> str | None:
        """The source of the declaration at `line` naming `name`, decorators and template
        head included: its own bytes, not the lines it shares with others. None when the
        line holds no such declaration or several."""
        name = unnumbered(name)
        at = self._at(line, name)
        return None if at is None else self._text_at(at, name)

    def shown(self, line: int, name: str) -> str | None:
        """The declaration at `line` as the judge reads it (`_member`); None as `text`."""
        name = unnumbered(name)
        at = self._at(line, name)
        return None if at is None else self._member(at, name, False).shown

    def _text_at(self, at: tuple[Any, Any], name: str) -> str:
        return self._member(at, name, False).text

    def _member(self, at: tuple[Any, Any], name: str, shapes: bool) -> _Member:
        """Its own bytes under the conditions that select it, as its fingerprint covers
        them and as the judge reads them: one `(selected when: ...)` line a condition,
        then the code as written, from the indent of its first line."""
        guard, own = self._guard(at[1]), self._own_text(at, name)
        conditions = "".join(f"(selected when: {g})\n" for g in guard.splitlines())
        start = at[0].start_byte
        indent = self.source[self.source.rfind(b"\n", 0, start) + 1 : start]
        shown = conditions + (indent.decode() if indent.isspace() else "") + own
        shape = self._shape_at(at, name) if shapes else NO_SHAPE
        printed = self._printed(at, name, own)
        return _Member(
            f"{guard}\n{own}" if guard else own,
            f"{guard}\n{printed}" if guard else printed,
            shape,
            shown,
        )

    def _printed(self, at: tuple[Any, Any], name: str, own: str) -> str:
        """What a declaration's fingerprint covers: its own text in Python, where layout
        is behaviour; elsewhere its tokens as the parser nests them, so a reformat or a
        comment changes nothing while a literal's bytes still do."""
        found = self._declared(at[1], name)
        if self.grammar == "python" or found is None:
            return own
        skip = {_key(n) for n in [*_sibling_nodes(found[0]), *_comments_in(*at)]}
        leaves, marks = _leaves(*at, _Fields(), skip)
        texts = [self.source[x.start_byte : x.end_byte].decode("utf-8", "replace") for x in leaves]
        return "\n".join([*(f"{m} {t}" for m, t in zip(marks, texts, strict=False)), marks[-1]])

    def _own_text(self, at: tuple[Any, Any], name: str) -> str:
        found = self._declared(at[1], name)
        out, pos = [], at[0].start_byte
        # each comment from the end of the node before it
        comments = [
            (c.prev_sibling.end_byte if c.prev_sibling else c.start_byte, c.end_byte)
            for c in _comments_within(*at)
        ]
        for start, end in sorted(_sibling_spans(found[0] if found else None) + comments):
            out.append(self.source[pos:start])
            pos = end
        out.append(self.source[pos : at[1].end_byte])
        return b"".join(out).decode("utf-8", "replace")

    def _guard(self, node: Any) -> str:
        """The conditions that select `node`, outermost first, one a line: `#if X` over
        it, `not #if X` when it is in a later branch; '' when none."""
        levels = []
        child, parent = node, node.parent
        while parent is not None:
            if parent.type in _CONDITIONALS and not self._include_guard(parent):
                # a Python `elif` chain is flat: the clauses before this one are passed too
                later = parent.children_by_field_name("alternative")
                passed = next((later[:i] for i, c in enumerate(later) if _same(c, child)), None)
                if passed is None:
                    levels.append([self._head(parent)])
                else:
                    levels.append([f"not {h}" for h in map(self._head, [parent, *passed]) if h])
            elif parent.type == "try_statement" and child.type in _HANDLERS:
                # an `except` runs when the handlers before it do not match
                level = []
                for c in parent.children:
                    if _same(c, child):
                        break
                    if c.type in _HANDLERS:
                        level.append(f"not {self._handler(c)}")
                levels.append([*level, self._handler(child)])
            child, parent = parent, parent.parent
        return "\n".join(h for level in reversed(levels) for h in level)

    def _head(self, node: Any) -> str:
        """`#if X`, `#ifndef X`, `elif X`: a branch's keyword and condition tokens, its
        comments and line breaks left out; '' for an `else`."""
        cond = node.child_by_field_name("condition") or node.child_by_field_name("name")
        if cond is None:
            return ""
        # the keyword by its token type: `#  if` is `#if`
        return " ".join([node.children[0].type, *(self._text(t) for t in _tokens(cond))])

    def _handler(self, clause: Any) -> str:
        """`except ValueError`: an `except` clause's keyword and the types it catches; the
        name `as` binds does not choose it."""
        words: list[str] = []
        for c in clause.children:
            if c.type in ("block", ":"):
                continue
            caught = c.named_children[0] if c.type == "as_pattern" else c
            words.extend(self._text(t) for t in _tokens(caught))
        return " ".join(words)

    def _include_guard(self, node: Any) -> bool:
        """`#ifndef X` or `#if !defined(X)`, then `#define X`, around a header: no
        condition on what it holds."""
        if node.parent.type != "translation_unit" or node.child_by_field_name("alternative"):
            return False
        if node.type == "preproc_ifdef" and node.children[0].type == "#ifndef":
            head = macro = node.child_by_field_name("name")
        elif node.type == "preproc_if":
            head = node.child_by_field_name("condition")
            macro = _undefined(head)
        else:
            return False
        define = head.next_named_sibling if macro is not None else None
        while define is not None and define.type in _COMMENTS:
            define = define.next_named_sibling
        return (
            define is not None
            and define.type == "preproc_def"
            and self._text(define.child_by_field_name("name")) == self._text(macro)
        )

    def fingerprints(
        self, lines: Sequence[int], name: str, *, shapes: bool = True
    ) -> Prints | None:
        """The fingerprint, rename shape and judge's text over every declaration of the
        name at `lines` in its scope (overloads, a declaration and its implementation,
        `#if` branches), in source order; None when one cannot be singled out."""
        keys = self._family_keys(lines, name, shapes)
        if keys is None:
            return None
        if keys not in self._prints:
            members = [m for key in keys for m in self._families[key]]
            body = fingerprint([m.printed for m in members])
            shown = "\n\n".join(m.shown for m in members)
            self._prints[keys] = Prints(body, joined([m.shape for m in members]), shown)
        return self._prints[keys]

    def _family_keys(
        self, lines: Sequence[int], name: str, shapes: bool
    ) -> tuple[tuple[Any, ...], ...] | None:
        keys = []
        for line in lines:
            if (key := self._family_key(line, name, shapes)) is None:
                return None
            keys.append(key)
        return tuple(dict.fromkeys(keys))

    def _family_key(self, line: int, name: str, shapes: bool) -> tuple[Any, ...] | None:
        """The scope and spelling of the declaration at `line`, its family read into
        `_families` once per scope and spelling, for every ordinal."""
        name = unnumbered(name)
        at = self._at(line, name)
        found = self._declared(at[1], name) if at else None
        if at is None or found is None or at[1].parent is None:
            return None
        scope = _chain(at[1].parent)
        key = (*_key(scope), _spelled(found[1]), name, shapes)
        if key not in self._families:
            out = []
            for node in self._members(scope).get(_spelled(found[1]), []):
                out.append(self._member(_prefixed(node), name, shapes))
            self._families[key] = out
        return key

    def _members(self, scope: Any) -> dict[str, list[Any]]:
        """spelling -> the declarations directly in `scope` naming it (in each branch of an
        `#if` chain), read once per scope."""
        if (known := self._scopes.get(_key(scope))) is not None:
            return known
        out: dict[str, list[Any]] = defaultdict(list)
        for c in _branches(scope):
            decl = _inner(c)
            if decl.type in _VARIABLES:
                names = [d.child_by_field_name("name") for d in decl.named_children]
            elif decl.type in _DECLARATIONS:
                names = [_name_node(decl)]
            else:
                continue
            for n in names:
                if n is not None and not any(_same(c, x) for x in out[_spelled(n)]):
                    out[_spelled(n)].append(c)
        self._scopes[_key(scope)] = out
        return out

    def shape(self, line: int, name: str) -> Shape:
        """The rename shapes of the declaration at `line`; `NO_SHAPE` when the
        declaration or its name node cannot be found. A bare call inside a method only
        means the method in C++ and Java, so elsewhere a method that calls its own name
        has no exact shape."""
        name = unnumbered(name)  # `LoomClient#2`: the index's second declaration of it
        at = self._at(line, name)
        return NO_SHAPE if at is None else self._shape_at(at, name)

    def _shape_at(self, at: tuple[Any, Any], name: str) -> Shape:
        found = self._declared(at[1], name)
        if found is None:
            return NO_SHAPE
        decl, named, body = found
        guard = self._guard(at[1])
        fields = _Fields()
        skip = {_key(n) for n in [*_sibling_nodes(decl), *_comments_within(*at)]}
        leaves, marks = _leaves(*at, fields, skip)
        texts = [self.source[x.start_byte : x.end_byte].decode("utf-8", "replace") for x in leaves]
        uses = {
            i: _use(x, self.grammar, decl, body, fields)
            for i, x in enumerate(leaves)
            if texts[i] == name and not _same(x, named) and _within(x, decl)
        }
        sure = not {"other", "call?", "read?"} & set(uses.values()) and (
            not uses or self.grammar in ("cpp", "java") or not _is_method(decl)
        )

        def form(blanked: set[str]) -> str:
            tokens = [
                marks[i] + " " + ("_" if _same(x, named) or uses.get(i) in blanked else texts[i])
                for i, x in enumerate(leaves)
            ]
            tokens.append(marks[-1])
            tokens.insert(0, guard)
            return hashlib.sha256("\0".join(tokens).encode()).hexdigest()

        both = form({"call", "read", "call?", "read?"})
        return Shape(both if sure else "", form({"call", "call?"}), both)


def _undefined(cond: Any) -> Any:
    """`X` of a `!defined(X)` / `!defined X` condition; None for any other."""
    if cond.type != "unary_expression" or cond.children[0].type != "!":
        return None
    defined = cond.named_children[-1]
    return defined.named_children[0] if defined.type == "preproc_defined" else None


def _tokens(node: Any) -> Iterator[Any]:
    """The leaf tokens under `node` in order, comments skipped; a stack, as a long `||`
    chain nests as deep as it has terms."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in _COMMENTS:
            continue
        if n.child_count == 0:
            yield n
        stack.extend(reversed(n.children))


def _inner(node: Any) -> Any:
    """The declaration a decorator, template or export holder wraps."""
    while node.type in _HOLDERS:
        inner = (
            node.child_by_field_name("definition")
            or node.child_by_field_name("declaration")
            or (node.named_children or [None])[-1]
        )
        if inner is None:
            break
        node = inner
    return node


def _name_node(decl: Any) -> Any:
    """The token a declaration names itself with: its `name` field, or the innermost name
    of a C/C++ declarator (`Reader::read` -> `read`)."""
    named = decl.child_by_field_name("name")
    node = named if named is not None else decl.child_by_field_name("declarator")
    while node is not None and node.child_count and node.type not in _COMPOSITE_NAMES:
        node = (
            node.child_by_field_name("name")
            or node.child_by_field_name("declarator")
            # `T& f()`: a reference declarator holds the function declarator in no field
            or (node.named_children[-1] if node.type in _FIELDLESS_DECLARATORS else None)
        )
    return node


def _prefixed(node: Any) -> tuple[Any, Any]:
    """(first node, `node`): the decorators and attributes written as siblings before it."""
    first = node
    prev = node.prev_named_sibling
    while prev is not None and prev.type in _PREFIXES | _COMMENTS:
        if prev.type in _PREFIXES:
            first = prev
        prev = prev.prev_named_sibling
    return first, node


def _chain(scope: Any) -> Any:
    """`scope`, or the `#if` its `#elif` / `#else` branch belongs to: one name defined once
    per branch is one family."""
    while scope.type in _PREPROC_BRANCHES and scope.parent is not None:
        scope = scope.parent
    return scope


def _branches(scope: Any) -> list[Any]:
    """`scope`'s children, an `#if`'s with those of its `#elif` and `#else` branches."""
    out = []
    for c in scope.named_children:
        if scope.type in _PREPROC_IFS | _PREPROC_BRANCHES and c.type in _PREPROC_BRANCHES:
            out.extend(_branches(c))
        else:
            out.append(c)
    return out


def _spelled(named: Any) -> str:
    """A declared name as written, with the qualifier an out-of-line definition carries
    (`A::fetch_rows`) or a Go method's receiver type (`func (b *B) Read` is `B.Read`):
    two scopes' namesakes are no family."""
    node = named
    while node.parent is not None and node.parent.type in _QUALIFIED_NAMES:
        node = node.parent
    text = str(node.text.decode("utf-8", "replace"))
    decl = named.parent
    receiver = decl.child_by_field_name("receiver") if decl is not None else None
    if receiver is not None and decl.type == "method_declaration":
        typ = next((p.child_by_field_name("type") for p in receiver.named_children), None)
        if typ is not None:
            receiver_type = str(typ.text.decode("utf-8", "replace")).lstrip("*").split("[")[0]
            return f"{receiver_type}.{text}"
    return text


def _sibling_nodes(decl: Any) -> list[Any]:
    """The other declarators of `const a = …, b = …` beside `decl`, the commas between
    them and the comments beside each (what lies between two commas goes with the
    declarator there): what `b` is says nothing about `a`."""
    parent = decl.parent if decl is not None and decl.type == "variable_declarator" else None
    if parent is None or parent.type not in _VARIABLES:
        return []
    # `var a = …, a = …`: the last one binds it; nothing is cut, so the whole reads
    name = (decl.child_by_field_name("name") or decl).text
    if any(
        c.type == "variable_declarator"
        and not _same(c, decl)
        and (c.child_by_field_name("name") or c).text == name
        for c in parent.children
    ):
        return []
    out: list[Any] = []
    segment: list[Any] = []
    for c in [*parent.children, None]:
        if c is None or c.type == ",":
            if any(x.type == "variable_declarator" and not _same(x, decl) for x in segment):
                out.extend(x for x in segment if x.type in ("variable_declarator", "comment"))
            segment = []
            if c is not None:
                out.append(c)
        else:
            segment.append(c)
    return out


def _comments_in(first: Any, last: Any) -> list[Any]:
    """Every comment from `first` to `last`, each a whole node (a Rust block comment has
    its delimiters as children)."""
    out, stack, n = [], [], first
    while n is not None:
        stack.append(n)
        if n == last:
            break
        n = n.next_sibling
    while stack:
        n = stack.pop()
        if n.type in _COMMENTS:
            out.append(n)
        else:
            stack.extend(n.children)
    return out


def _comments_within(first: Any, node: Any) -> list[Any]:
    """The comments from `first` up to the declaration's own first token, past its
    decorators, annotations, attributes and template head: a comment above a definition
    is no part of it, wherever the grammar puts it (`@Override // c public int f`)."""
    stack, n = [], first
    while n is not None and n.start_byte <= node.start_byte:
        stack.append((n, False))
        n = n.next_named_sibling
    stack.reverse()
    out = []
    while stack:
        n, in_head = stack.pop()
        if n.type in _COMMENTS:
            out.append(n)
        elif n.child_count:
            in_head = in_head or n.type in _HEADS
            stack.extend((c, in_head) for c in reversed(n.children))
        elif not (in_head or n.type in _HEADS or _exported(n)):
            return out  # the declaration's own first token
    return out


def _exported(token: Any) -> bool:
    """The `export` or `default` keyword of an export statement (`export @dec() class`,
    `export /* c */ function f`): it stands before the declaration's own first token."""
    parent = token.parent
    return (
        parent is not None
        and parent.type == "export_statement"
        and token.type in ("export", "default")
    )


def _sibling_spans(decl: Any) -> list[tuple[int, int]]:
    """The byte ranges of `_sibling_nodes`, each from the end of the node before it, so
    `const a = 1, b = 2;` reads `const a = 1;` whichever side `a` is on."""
    cut = {_key(n) for n in _sibling_nodes(decl)}
    if not cut:
        return []
    out: list[tuple[int, int]] = []
    prev = None
    for c in decl.parent.children:
        if _key(c) in cut:
            start = prev.end_byte if prev is not None else c.start_byte
            if out and out[-1][1] >= start:
                out[-1] = (out[-1][0], c.end_byte)
            else:
                out.append((start, c.end_byte))
        prev = c
    return out


def _leaves(
    first: Any, last: Any, fields: _Fields, skip: set[tuple[int, int, str]] | None = None
) -> tuple[list[Any], list[str]]:
    """The tokens from `first` to `last`, each with the nodes opened and closed before it
    (so the same tokens nested differently differ); records each node's parent and
    previous sibling in `fields`."""
    out: list[Any] = []
    marks: list[str] = []
    pending: list[str] = []
    node = first
    while node is not None:
        stack: list[tuple[Any, bool]] = [(node, False)]
        while stack:
            n, closing = stack.pop()
            if skip and _key(n) in skip:
                continue
            if closing:
                pending.append(")")
            elif n.child_count == 0:
                out.append(n)
                marks.append("".join(pending) + n.type)
                pending.clear()
            else:
                pending.append("(" + n.type)
                stack.append((n, True))
                children = n.children
                for i, c in enumerate(children):
                    fields.around[_key(c)] = (n, children[i - 1] if i else None)
                stack.extend((c, False) for c in reversed(children))
        if node == last:
            break
        node = node.next_sibling
    marks.append("".join(pending))
    return out, marks


def _within(node: Any, outer: Any) -> bool:
    return bool(outer.start_byte <= node.start_byte and node.end_byte <= outer.end_byte)


def _same(a: Any, b: Any) -> bool:
    return bool(a is not None and (a.start_byte, a.end_byte) == (b.start_byte, b.end_byte))


def _is_method(decl: Any) -> bool:
    if decl.type == "method_declaration":  # Go: a receiver's method
        return True
    node = decl.parent
    while node is not None:
        if node.type in _CLASS_NODES:
            return True
        if node.type in _FUNCTIONS:
            return False
        node = node.parent
    return False


def _use(leaf: Any, grammar: str, decl: Any, body: Any, fields: _Fields) -> str | None:
    """How an occurrence of the declared name is used: 'call', 'read', 'call?'/'read?'
    (inside a nested class, where it may mean a member), 'other' (a binding, a parameter,
    anything not known to be a call or read), or None when it names something else."""
    if decl.type in _CLASSES and body is not None and _within(leaf, body):
        prev = fields.prev(leaf)
        if prev is None or prev.type not in _ACCESS:
            return "read"  # the class itself: `-> Reader`, `x: Reader`, `Reader()`
    if leaf.type == "type_identifier":  # a type of that name (`using NAME = Box;`, a
        # TypeScript class) may be what a bare call or read means; Java keeps types apart
        prev = fields.prev(leaf)
        bare = prev is None or prev.type not in _ACCESS
        return "other" if bare and grammar != "java" else None
    if not _bare_reference(leaf, fields):
        # `using backend::NAME;`, `use crate::a::NAME;` bind the bare name all the same
        path = leaf.type == "identifier" and fields.prev(leaf).type == "::"
        return "other" if path and _imported(leaf, decl, fields) == "binding" else None
    nested, node = False, fields.parent(leaf)
    while node is not None and _within(node, decl) and not _same(node, decl):
        nested = nested or node.type in _NESTED_CLASSES
        node = fields.parent(node)
    kind = _kind(leaf, grammar, decl, body, fields)
    return kind + "?" if nested and kind in ("call", "read") else kind


def _kind(leaf: Any, grammar: str, decl: Any, body: Any, fields: _Fields) -> str | None:
    parent = fields.parent(leaf)
    field = fields.of(parent, leaf)
    if grammar == "rust" and parent.type == "macro_invocation":
        return None
    if grammar == "java":  # methods and variables are separate namespaces
        bare_call = parent.type == "method_invocation" and field == "name"
        return "call" if bare_call and parent.child_by_field_name("object") is None else None
    if (parent.type, field) == ("keyword_argument", "name") or (
        parent.type == "keyed_element" and _same(parent.named_children[0], leaf)
    ):
        return None
    if (role := _imported(leaf, decl, fields)) is not None:
        return "other" if role == "binding" else None
    if body is None or not _within(leaf, body):
        return "other"
    if parent.type in _CALLS and field == "function":
        return "call"
    if parent.type in _READS or (parent.type, field) in _READ_FIELDS:
        return "read"
    if parent.type == "expression_list" and parent.parent is not None:  # Go
        outer = (parent.parent.type, fields.of(parent.parent, parent))
        if outer[0] == "return_statement" or outer in {
            ("short_var_declaration", "right"),
            ("assignment_statement", "right"),
        }:
            return "read"
    return "other"


def _imported(leaf: Any, decl: Any, fields: _Fields) -> str | None:
    """`leaf`'s role in the import it is part of ('binding' or 'label'); None outside
    an import."""
    parent = fields.parent(leaf)
    if not fields.in_import(parent, decl):
        return None
    path, node = [(parent, leaf)], parent
    while node is not None and node.type not in _IMPORTS:
        up = fields.parent(node)
        path.append((up, node))
        node = up
    return _import_role(leaf, path, fields)


def _bare_reference(leaf: Any, fields: _Fields) -> bool:
    """An `identifier` no access operator qualifies: not the member in `data.fetch_rows`
    or `data.fetch_rows()`, not the name in `other::fetch_rows`."""
    prev = fields.prev(leaf)
    return bool(leaf.type == "identifier" and (prev is None or prev.type not in _ACCESS))


def _key(node: Any) -> tuple[int, int, str]:
    return node.start_byte, node.end_byte, node.type


class _Fields:
    """The field each child of a node is in, and whether a node is inside an import of
    the declaration: each read once per node."""

    def __init__(self) -> None:
        self._by_node: dict[tuple[int, int, str], dict[tuple[int, int], str]] = {}
        self._imports: dict[tuple[int, int, str], bool] = {}
        self.around: dict[tuple[int, int, str], tuple[Any, Any]] = {}  # (parent, prev)

    def parent(self, node: Any) -> Any:
        known = self.around.get(_key(node))
        return known[0] if known else node.parent

    def prev(self, node: Any) -> Any:
        known = self.around.get(_key(node))
        return known[1] if known else node.prev_sibling

    def in_import(self, node: Any, decl: Any) -> bool:
        seen: list[tuple[int, int, str]] = []
        found = False
        while node is not None and _within(node, decl):
            key = _key(node)
            if key in self._imports:
                found = self._imports[key]
                break
            seen.append(key)
            if node.type in _IMPORTS:
                found = True
                break
            node = self.parent(node)
        for key in seen:
            self._imports[key] = found
        return found

    def of(self, node: Any, child: Any) -> str:
        key = _key(node)
        if (fields := self._by_node.get(key)) is None:
            fields = self._by_node[key] = {
                (c.start_byte, c.end_byte): node.field_name_for_child(i) or ""
                for i, c in enumerate(node.children)
            }
        return fields.get((child.start_byte, child.end_byte), "")


def _import_role(leaf: Any, path: list[tuple[Any, Any]], fields: _Fields) -> str:
    """'binding' for the name an import declares (`DEFAULT` in `from fetch_rows import
    DEFAULT`, `handler` in `fetch_rows as handler`, `a` in `import a.b`, the last part of
    a `use`/`import`/`using` path), else 'label': a module or a source name is neither a
    binding nor a use."""
    trail = [(node, fields.of(node, child)) for node, child in path]
    for node, field in trail:
        if node.type in _ALIASED:
            alias = _ALIASED[node.type]
            if node.child_by_field_name(alias) is not None:
                return "binding" if field == alias else "label"
            return "binding" if field == "name" else "label"
        if node.type == "import_spec":  # Go: only an explicit package name binds
            return "binding" if field == "name" else "label"
        if node.type == "import_from_statement":
            return "binding" if field == "name" else "label"
        if node.type == "import_statement" and trail[0][0].type == "dotted_name":
            first = trail[0][0].named_children[0]  # `import a.b` binds `a`
            return "binding" if _same(first, leaf) else "label"
    # a path (`use a::b::c`, `import java.util.List`, `using std::move`): its last part
    paths = [(n, f) for n, f in trail if n.type in _PATHS]
    if paths:
        outer, _ = paths[-1]
        inside_path = any(n.type == "scoped_use_list" and f == "path" for n, f in trail)
        last = outer.child_by_field_name("name") or (outer.named_children or [None])[-1]
        return "binding" if not inside_path and _same(last, leaf) else "label"
    return "binding"  # a bare imported name: `use c`, a default import
