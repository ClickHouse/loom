"""A definition's declaration as its language parses it: where it starts once its
decorators, attributes and `template <...>` head are counted, and its rename shape (its
tokens with the declared name and the bare references to it blanked). A language with
no grammar here has neither: its text starts where the index says and it is never
matched as a rename."""

from __future__ import annotations

import hashlib
import inspect
import re
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from functools import lru_cache
from typing import Any, NamedTuple

from server.code.domain.anchor import NO_SHAPE, Shape, extension, fingerprint, language, unnumbered
from server.code.domain.extract import bounded_parse
from server.code.domain.extract.lang_typescript_nodes import (
    _is_function_like,
    _unwrap,
    _wrapped_function,
)

# the index's language -> its tree-sitter grammar (a notebook's is its code cells')
_GRAMMARS = {
    "python": "python",
    "cpp": "cpp",
    "typescript": "typescript",
    "javascript": "javascript",
    "go": "go",
    "java": "java",
    "rust": "rust",
}

# Declarations: where one starts, what it names, the conditions that select it.
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
_LAYOUT = _COMMENTS | {"line_continuation"}
# `const NAME = () => …`: a function bound to a variable, as the index declares it
_VARIABLES = {"lexical_declaration", "variable_declaration"}
_GO_GROUPS = {"type_declaration", "var_declaration", "const_declaration"}
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
_POINTERS = {"pointer_declarator", "reference_declarator"}
_QUALIFIED_NAMES = {"qualified_identifier", "scoped_identifier", "nested_name_specifier"}
_PREPROC_IFS = {"preproc_if", "preproc_ifdef"}
_PREPROC_BRANCHES = {"preproc_elif", "preproc_elifdef", "preproc_else"}
# what selects a branch: an `#if` or `if` condition; a later branch negates those before it
_CONDITIONALS = _PREPROC_IFS | {"preproc_elif", "preproc_elifdef", "if_statement", "elif_clause"}
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
_ECMA = {"typescript", "tsx", "javascript"}

# Rename shapes (`_use`): how an occurrence of the declared name is used.
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
    # `{ NAME }`: a JS / TS object's shorthand value, a Rust struct's
    "object",
    "shorthand_field_initializer",
}
# a name a destructuring binds without saying so
_SHORTHAND_BINDINGS = {"shorthand_property_identifier_pattern", "shorthand_field_identifier"}
# a bare name that can read a variable
_REFERENCES = {"identifier", "shorthand_property_identifier"}
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

# Fingerprints (`_leaves`): the tokens hashed and what a formatter changes.
_CLOSERS = {")", "]", "}"}
# a string's text, hashed as written in runs between its escapes and interpolations
_TEXTS = {"string_content", "format_specifier"}
# where a trailing comma's one element is a tuple (`(a,)`, Rust's `(T,)` type, Python's
# `x[a,]` index): it stays there unless another comma is in the node
_TUPLES = {"tuple", "tuple_type", "tuple_expression", "tuple_pattern", "subscript"}
# where a formatter adds and removes parentheses (`_unparenthesized`): elsewhere they are
# kept, as C++'s `decltype((x))` is not `decltype(x)`
_UNPARENTHESIZED = _ECMA | {"python"}
_CHAINS = {"member_expression", "call_expression", "subscript_expression"}
# a JS / TS `;` there separates members
_MEMBER_LISTS = {"class_body", "interface_body", "object_type"}
_FORS = {"for_statement", "for_in_statement"}
# where an empty JS / TS statement is a statement of its own, not a loop's or an `if`'s body
_STATEMENT_LISTS = {"program", "statement_block", "class_body", "switch_case", "switch_default"}
# a JS / TS type's members, whose `,` and `;` separators are alike
_TYPE_MEMBERS = {"interface_body", "object_type"}
# a Python tuple in parentheses reads as the bare one it can be written as: `return (a, b)`
_BARE_TUPLES = {"tuple": "expression_list", "tuple_pattern": "pattern_list"}
# number literals, whose letters' case means nothing: `0XFF` is `0xff`, `1E5` is `1e5`
_NUMBERS = {
    "integer",
    "float",
    "number",
    "number_literal",
    "integer_literal",
    "float_literal",
    "int_literal",
    "imaginary_literal",
    "decimal_integer_literal",
    "hex_integer_literal",
    "octal_integer_literal",
    "binary_integer_literal",
    "decimal_floating_point_literal",
    "hex_floating_point_literal",
}
_QUOTE_ESCAPES = {b"\\'", b'\\"'}
# what a Python docstring documents, beside a module
_DOCUMENTED = {"function_definition", "class_definition"}


def _bound_function(value: Any) -> Any:
    """The function a variable's value is, as the index reads one: through `(…)`, `as`,
    `satisfies`, `!` and wrapper calls; None when it is not one."""
    return _unwrap(value) if _is_function_like(value) else _wrapped_function(value)


def _joined(shapes: Sequence[Shape]) -> Shape:
    """One shape over several declarations of a name, each part '' when any of theirs is;
    `NO_SHAPE` over none."""
    if not shapes:
        return NO_SHAPE
    return Shape(*(fingerprint(part) if all(part) else "" for part in zip(*shapes, strict=True)))


class Member(NamedTuple):
    """One declaration: what its fingerprint covers, its rename shapes and the text the
    judge reads, the first and last under its conditions."""

    hashed: str
    shape: Shape
    shown: str


class Prints(NamedTuple):
    """A definition's fingerprints, and the text the judge reads of it."""

    body: str
    shape: Shape
    shown: str

    @classmethod
    def over(cls, members: Sequence[Member]) -> Prints:
        """The fingerprints and judge's text of `members` together, in order."""
        return cls(
            fingerprint([m.hashed for m in members]),
            _joined([m.shape for m in members]),
            "\n\n".join(m.shown for m in members),
        )


# Bump when fingerprints or shapes change for the same code: links hashed under another
# `fingerprint_version` are re-hashed. Never reuse a number: a stored link under it would
# read as current.
_RULES = 8


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
    """The tree-sitter grammar of `path` by the language the index parses it as; None
    for one without."""
    grammar = _GRAMMARS.get(language(path))
    return "tsx" if grammar == "typescript" and extension(path) == ".tsx" else grammar


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

    installed = []
    for package in ["tree-sitter", *sorted(f"tree-sitter-{g}" for g in set(_GRAMMARS.values()))]:
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
        self._families: dict[tuple[Any, ...], list[Member]] = {}
        self._prints: dict[tuple[tuple[Any, ...], ...], Prints] = {}
        self._cuts: dict[tuple[int, int, str], _Cut] = {}
        self._bindings: dict[tuple[int, int, str], dict[str, tuple[Any, Any, Any]]] = {}

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
                tree = bounded_parse.parse(parser, source)
            return cls(source, tree.root_node, grammar)
        except Exception:  # noqa: BLE001 — no grammar, no declaration: the index's span stands
            return None

    def _at(self, line: int, name: str | None = None) -> tuple[Any, Any] | None:
        """The definition that starts at `line`, as (its first node, its declaration):
        the first node is where its decorators, attributes or template head begin. With
        `name`, the one on that line declaring `name`. None when none or several do."""
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
            # holding more after it (a template's `;` after its class is no more)
            if parent.type in _SCOPES or not _ends(parent, node):
                break
            before = [c for c in parent.children if c.end_byte <= node.start_byte]
            wraps = parent.type in _HOLDERS or all(
                c.type in _PREFIXES | _COMMENTS or c.start_point[0] == line - 1 for c in before
            )
            if not wraps:
                break
            node = parent
        return _prefixed(node, self.grammar)

    def _sole_on(self, line: int, name: str, node: Any) -> Any:
        """The declaration on `line` that declares `name`; None when none or several do (one
        nested in another counts once)."""
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
        if (assigned := _assigned(decl)) is not None:
            return (*assigned, None) if self._text(assigned[1]) == name else None
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
        cursor = self.root.walk()
        while stack:
            n = stack.pop()
            if n.type in _DECLARATIONS and (named := _name_node(n)) is not None:
                if named.start_point[0] == row:
                    out[self._text(named)].append((n, named))
            # only the children on the row: a namespace body holds thousands of rows
            cursor.reset(n)
            if cursor.goto_first_child_for_point((row, 0)) is None:
                continue
            while cursor.node.start_point[0] <= row:
                stack.append(cursor.node)
                if not cursor.goto_next_sibling():
                    break
        self._rows[row] = out
        return out

    def _bound(self, decl: Any, name: str) -> tuple[Any, Any, Any] | None:
        """(declarator, name, body) of the declarator binding `name`: to a function as the
        index reads one (through `(…)`, `as`, `satisfies`, `!` and wrapper calls), else
        to a value, with no body. Read once per declaration."""
        if (known := self._bindings.get(_key(decl))) is None:
            values: dict[str, tuple[Any, Any, Any]] = {}
            functions: dict[str, tuple[Any, Any, Any]] = {}
            for d in decl.named_children:
                n, v = d.child_by_field_name("name"), d.child_by_field_name("value")
                if d.type != "variable_declarator" or n is None:
                    continue
                fn = None if v is None else _bound_function(v)
                if fn is None:
                    values.setdefault(self._text(n), (d, n, None))
                else:
                    functions.setdefault(self._text(n), (d, n, fn.child_by_field_name("body")))
            known = self._bindings[_key(decl)] = values | functions
        return known.get(name)

    def _text(self, node: Any) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def start(self, line: int) -> int:
        """The line the definition at `line` starts on, its decorators counted."""
        at = self._at(line)
        return at[0].start_point[0] + 1 if at else line

    def member(self, line: int, name: str, *, shapes: bool = True) -> Member | None:
        """The declaration at `line` naming `name` (`LoomClient#2`: the index's second
        one), decorators and template head included; its rename shapes only with
        `shapes`. None when the line holds no such declaration or several."""
        name = unnumbered(name)
        at = self._at(line, name)
        return None if at is None else self._member(at, name, shapes)

    def _member(self, at: tuple[Any, Any], name: str, shapes: bool) -> Member:
        """Its own bytes (not the lines it shares with others) under the conditions that
        select it, as its fingerprint covers them and as the judge reads them: one
        `(selected when: ...)` line a condition, then the code as written, from the
        indent of its first line."""
        found = self._declared(at[1], name)
        guard, own = self._guard(at[1]), self._own_text(at, found)
        conditions = "".join(f"(selected when: {g})\n" for g in guard.splitlines())
        start = at[0].start_byte
        indent = self.source[self.source.rfind(b"\n", 0, start) + 1 : start]
        shown = conditions + (indent.decode() if indent.isspace() else "") + own
        if found is None:
            hashed, shape = own, NO_SHAPE
        else:
            tokens = self._tokens(at, found[0])
            hashed = _hashed(tokens)
            shape = self._shape(found, name, guard, tokens) if shapes else NO_SHAPE
        return Member(f"{guard}\n{hashed}" if guard else hashed, shape, shown)

    def _tokens(self, at: tuple[Any, Any], decl: Any) -> _Tokens:
        fields = _Fields()
        kept = None if (cut := self._cut(decl)) is None else (decl.parent, cut.kept(decl))
        leaves, marks = _leaves(*at, fields, self.source, self.grammar, kept)
        texts = [
            x.text if isinstance(x, _Gap) else _quote(x, self._text(x), self.grammar)
            for x in leaves
        ]
        return _Tokens(leaves, marks, texts, fields)

    def _own_text(self, at: tuple[Any, Any], found: tuple[Any, Any, Any] | None) -> str:
        out, pos = [], at[0].start_byte
        # each comment from the end of the node before it
        comments = [
            (c.prev_sibling.end_byte if c.prev_sibling else c.start_byte, c.end_byte)
            for c in _comments_within(*at)
            if not _directive(c, self.grammar)
        ]
        cut = self._cut(found[0]) if found else None
        siblings = cut.spans(found[0]) if cut is not None and found else []
        for start, end in sorted(siblings + comments):
            out.append(self.source[pos:start])
            pos = end
        out.append(self.source[pos : at[1].end_byte])
        return b"".join(out).decode("utf-8", "replace")

    def _cut(self, decl: Any) -> _Cut | None:
        """The declarators of `const a = …, b = …` that `decl` is one of, read once per
        declaration; None for any other node."""
        parent = decl.parent if decl.type == "variable_declarator" else None
        if parent is None or parent.type not in _VARIABLES:
            return None
        if (key := _key(parent)) not in self._cuts:
            self._cuts[key] = _Cut(parent)
        return self._cuts[key]

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
            self._prints[keys] = Prints.over([m for key in keys for m in self._families[key]])
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
                out.append(self._member(_prefixed(node, self.grammar), name, shapes))
            self._families[key] = out
        return key if self._families[key] else None

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
            elif decl.type in _DECLARATIONS or _prototype(decl):
                names = [_name_node(decl)]
            elif (assigned := _assigned(decl)) is not None:
                names = [assigned[1]]
            else:
                continue
            for n in names:
                if n is not None and not any(_same(c, x) for x in out[_spelled(n)]):
                    out[_spelled(n)].append(c)
        self._scopes[_key(scope)] = out
        return out

    def _shape(self, found: tuple[Any, Any, Any], name: str, guard: str, tokens: _Tokens) -> Shape:
        """Its rename shapes. A bare call inside a method only means the method in C++ and
        Java, so elsewhere a method that calls its own name has no exact shape."""
        decl, named, body = found
        leaves, marks, texts, fields = tokens
        uses = {
            i: _use(x, self.grammar, decl, body, fields)
            for i, x in enumerate(leaves)
            if texts[i] == name and not _same(x, named) and _within(x, decl)
        }
        sure = not {"other", "call?", "read?"} & set(uses.values()) and (
            not uses or self.grammar in ("cpp", "java") or not _is_method(decl)
        )

        def form(blanked: set[str]) -> str:
            out = [
                marks[i] + " " + ("_" if _same(x, named) or uses.get(i) in blanked else texts[i])
                for i, x in enumerate(leaves)
            ]
            out.append(marks[-1])
            out.insert(0, guard)
            return hashlib.sha256("\0".join(out).encode()).hexdigest()

        both = form({"call", "read", "call?", "read?"})
        return Shape(both if sure else "", form({"call", "call?"}), both)


class _Tokens(NamedTuple):
    """A declaration's tokens (`_leaves`), the nesting marks before each and their text."""

    leaves: list[Any]
    marks: list[str]
    texts: list[str]
    fields: _Fields


def _hashed(tokens: _Tokens) -> str:
    """Its tokens as the parser nests them, less comments and what formatters change;
    parentheses are kept in C++, Go, Rust and Java."""
    _, marks, texts, _ = tokens
    return "\n".join([*(f"{m} {t}" for m, t in zip(marks, texts, strict=False)), marks[-1]])


def _ends(parent: Any, node: Any) -> bool:
    """`parent` ends with `node`, or is a holder with only a `;` after it."""
    if parent.end_byte == node.end_byte:
        return True
    if parent.type not in _HOLDERS:
        return False
    return all(c.type == ";" for c in parent.children if c.start_byte >= node.end_byte)


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
    """The declaration a decorator, template or export holder wraps, or the one spec of a
    Go `type`, `var` or `const` declaration."""
    while node.type in _HOLDERS or node.type in _GO_GROUPS:
        if node.type in _GO_GROUPS:
            specs = [c for c in node.named_children if c.type not in _COMMENTS]
            if len(specs) != 1 or specs[0].type not in _DECLARATIONS:
                break
            node = specs[0]
            continue
        inner = (
            node.child_by_field_name("definition")
            or node.child_by_field_name("declaration")
            or (node.named_children or [None])[-1]
        )
        if inner is None:
            break
        node = inner
    return node


def _prototype(node: Any) -> bool:
    """`node` is a C / C++ function's prototype: `int f(int a = 1);`."""
    if node.type != "declaration":
        return False
    d = node.child_by_field_name("declarator")
    while d is not None and d.type in _POINTERS:
        d = d.child_by_field_name("declarator") or (d.named_children or [None])[-1]
    return d is not None and d.type == "function_declarator"


def _assigned(node: Any) -> tuple[Any, Any] | None:
    """(assignment, name) of a Python `NAME = …` statement, a module's or a class's
    constant; None for any other node."""
    if node.type != "expression_statement" or node.named_child_count != 1:
        return None
    assignment = node.named_children[0]
    left = assignment.child_by_field_name("left") if assignment.type == "assignment" else None
    return (assignment, left) if left is not None and left.type == "identifier" else None


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


def _prefixed(node: Any, grammar: str) -> tuple[Any, Any]:
    """(first node, `node`): the decorators, attributes and Go directives written as
    siblings before it."""
    first = node
    prev = node.prev_named_sibling
    while prev is not None and prev.type in _PREFIXES | _COMMENTS:
        if prev.type in _PREFIXES or grammar == "go" and _directive(prev, grammar):
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


def _comments_within(first: Any, node: Any) -> list[Any]:
    """The comments among a declaration's decorators, attributes and template head: from
    `first` up to the first token of `node` itself."""
    out = []
    # (node, inside a head, among `first`'s siblings): children are read only as reached
    stack = [(first, False, True)]
    while stack:
        n, in_head, top = stack.pop()
        after = n.next_named_sibling if top else n.next_sibling
        if after is not None and (not top or after.start_byte <= node.start_byte):
            stack.append((after, in_head, top))
        if n.type in _COMMENTS:
            out.append(n)
        elif n.child_count:
            stack.append((n.child(0), in_head or n.type in _HEADS, False))
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


_CUT_TYPES = {"variable_declarator", "comment"}


class _Cut:
    """Splits `const a = …, b = …` so each declarator reads alone, as if declared on its
    own: what lies between two commas, comments too, goes with the declarator there."""

    def __init__(self, parent: Any) -> None:
        self.children = parent.children
        # `var a = …, a = …`: the last one binds it; nothing is cut, so the whole reads
        names = Counter(
            (c.child_by_field_name("name") or c).text
            for c in self.children
            if c.type == "variable_declarator"
        )
        self._repeated = {n for n, k in names.items() if k > 1}
        self._own: dict[tuple[int, int, str], list[int]] = {}
        self._fixed: list[int] = []  # what every declarator reads: the keyword, the `;`
        holders = 0
        segment: list[int] = []
        for i, c in enumerate([*self.children, None]):
            if c is not None and c.type != ",":
                segment.append(i)
                continue
            mine = [j for j in segment if self.children[j].type in _CUT_TYPES]
            declarators = [j for j in mine if self.children[j].type == "variable_declarator"]
            if declarators:
                holders += 1
                self._own[_key(self.children[declarators[0]])] = mine
                self._fixed.extend(j for j in segment if j not in mine)
            else:
                self._fixed.extend(segment)
            segment = []
        self._cuts = holders > 1

    def _indexes(self, decl: Any) -> list[int] | None:
        """The indexes of the children `decl` reads; None when it reads them all."""
        if not self._cuts or (decl.child_by_field_name("name") or decl).text in self._repeated:
            return None
        return sorted([*self._fixed, *self._own.get(_key(decl), [])])

    def kept(self, decl: Any) -> list[Any]:
        """The children `decl` reads."""
        kept = self._indexes(decl)
        return list(self.children) if kept is None else [self.children[i] for i in kept]

    def spans(self, decl: Any) -> list[tuple[int, int]]:
        """The byte ranges `decl` does not read, each cut child from the end of the node
        before it, so `const a = 1, b = 2;` reads `const a = 1;` whichever side `a` is on."""
        kept = self._indexes(decl)
        if kept is None:
            return []
        out = []
        prev = -1
        for i in [*kept, len(self.children)]:
            if i > prev + 1:  # children prev+1 .. i-1 are cut
                start = self.children[prev].end_byte if prev >= 0 else self.children[0].start_byte
                out.append((start, self.children[i - 1].end_byte))
            prev = i
        return out


class _Gap(NamedTuple):
    """Text hashed as a token of its own: what no child of a node covers (a Python
    string's text beside an escape, an f-string's format spec), a string's run of text,
    a docstring. It stands in for a tree-sitter leaf, with the fields of one that
    `_leaves` and `_use` read."""

    start_byte: int
    end_byte: int
    text: str
    type: str = "gap"
    child_count: int = 0
    parent: Any = None
    prev_sibling: Any = None


def _gaps(node: Any, source: bytes, grammar: str) -> list[Any]:
    """`node`'s children in order with the text between them that none covers, trimmed
    and only when not blank; a string's text as `_runs` reads it, a docstring as
    `_docstring` does."""
    if grammar == "python" and (doc := _docstring(node, source)) is not None:
        return doc
    if node.type in _TEXTS or grammar in _ECMA and node.type == "string":
        return _runs(node, source)
    out: list[Any] = []
    pos = node.start_byte
    for c in [*node.children, None]:
        end = node.end_byte if c is None else c.start_byte
        gap = source[pos:end]
        if text := gap.strip():
            lead = len(gap) - len(gap.lstrip())
            out.append(_Gap(pos + lead, pos + lead + len(text), _decoded(text)))
        if c is not None:
            pos = c.end_byte
            if c.type == "=" and node.type == "interpolation":
                # `{x + 1 = }` prints its expression and the spaces around `=` as written
                after = c.next_sibling.start_byte if c.next_sibling else c.end_byte
                c = _Gap(c.start_byte, c.end_byte, _decoded(source[node.start_byte + 1 : after]))
            out.append(c)
    return out


def _runs(node: Any, source: bytes) -> list[Any]:
    """A string's children with each run of its text as one token, as written but for a
    quote's escape, which reads as the quote: `'it\\'s'` is `"it's"`."""
    out: list[Any] = []
    run: list[bytes] = []
    start = pos = node.start_byte
    for c in [*node.children, None]:
        end = node.end_byte if c is None else c.start_byte
        run.append(source[pos:end])
        text = source[c.start_byte : c.end_byte] if c is not None else b""
        if text in _QUOTE_ESCAPES or c is not None and c.type == "string_fragment":
            run.append(text[-1:] if text in _QUOTE_ESCAPES else text)
            pos = c.end_byte
            continue
        if joined := b"".join(run):
            out.append(_Gap(start, end, _decoded(joined)))
        run = []
        if c is not None:
            out.append(c)
            start = pos = c.end_byte
    return out


def _docstring(node: Any, source: bytes) -> list[Any] | None:
    """A Python docstring's quotes and its text as `inspect.cleandoc` reads it, each line's
    trailing spaces dropped: re-indented or moved, it reads the same. None for any other
    node."""
    stmt = node.parent
    if node.type != "string" or stmt is None or stmt.type != "expression_statement":
        return None
    body = stmt.parent
    owner = body.parent.type if body.parent is not None else None
    if not (body.type == "module" or body.type == "block" and owner in _DOCUMENTED):
        return None
    first = next((c for c in body.named_children if c.type not in _COMMENTS), None)
    opener, *inner, closer = node.children
    if not _same(first, stmt) or any(c.type == "interpolation" for c in inner):
        return None
    text = inspect.cleandoc(_decoded(source[opener.end_byte : closer.start_byte]))
    cleaned = "\n".join(line.rstrip() for line in text.splitlines())
    return [opener, _Gap(opener.end_byte, closer.start_byte, cleaned), closer]


def _decoded(text: bytes) -> str:
    return text.decode("utf-8", "replace")


def _leaves(
    first: Any,
    last: Any,
    fields: _Fields,
    source: bytes,
    grammar: str = "",
    kept: tuple[Any, list[Any]] | None = None,
) -> tuple[list[Any], list[str]]:
    """The tokens from `first` to `last` but the layout (comments, Python's `\\` line
    continuations) and those a formatter adds or removes (`_formatting`), with the text no
    token covers or that reads other than as written (`_gaps`, each a `_Gap` standing in
    for a leaf), each with the nodes opened and closed before it (so the same tokens
    nested differently differ); records each node's parent and previous sibling in
    `fields`. `kept` is (a node, the children of it read): `_Cut.kept`."""
    out: list[Any] = []
    marks: list[str] = []
    pending: list[str] = []
    node = first
    while node is not None:
        stack: list[tuple[Any, bool]] = [(node, False)]
        while stack:
            n, closing = stack.pop()
            if not closing and (_layout(n, grammar) or _formatting(n, grammar)):
                continue
            if closing:
                pending.append(")")
            elif n.child_count == 0 and n.type not in _TEXTS:  # a text reads as its runs
                out.append(n)
                marks.append("".join(pending) + ('"' if _js_quote(n, grammar) else n.type))
                pending.clear()
            else:
                bare = grammar == "python" and n.type in _BARE_TUPLES
                pending.append("(" + (_BARE_TUPLES[n.type] if bare else n.type))
                stack.append((n, True))
                cut = kept[1] if kept is not None and _same(n, kept[0]) else None
                children = n.children if cut is None else cut
                for i, c in enumerate(children):
                    fields.around[_key(c)] = (n, children[i - 1] if i else None)
                within = _gaps(n, source, grammar) if cut is None else cut
                stack.extend((_unparenthesized(c, grammar), False) for c in reversed(within))
        if node == last:
            break
        node = node.next_sibling
    marks.append("".join(pending))
    return out, marks


def _unparenthesized(node: Any, grammar: str) -> Any:
    """`node` without what a formatter adds or removes around it: parentheses around one
    Python, JS or TS expression (not one that ends an optional chain, `(a?.b).c`, nor a
    JS / TS statement's lone string) or an arrow's one untyped parameter, a TS union's
    leading `|`."""
    statement = node.parent is not None and node.parent.type == "expression_statement"
    while True:
        inner = None
        if node.type == "parenthesized_expression" and grammar in _UNPARENTHESIZED:
            inner = _sole_child(node)
            parent = node.parent
            chained = parent is not None and parent.type in _CHAINS
            if chained and inner is not None and b"?." in inner.text:
                inner = None
            if statement and grammar in _ECMA and inner is not None and inner.type == "string":
                inner = None  # `("use strict");` is no directive, at any depth
        elif node.type == "formal_parameters" and grammar in _ECMA:
            arrow = node.parent is not None and node.parent.type == "arrow_function"
            inner = _sole_child(node) if arrow else None
            if inner is not None and inner.type == "required_parameter":
                inner = _sole_child(inner)
            if inner is not None and inner.type != "identifier":
                inner = None
        elif node.type in ("union_type", "intersection_type") and grammar in _ECMA:
            # `type U =\n  | A\n  | B` nests its leading `| A` as a union of one
            inner = _sole_child(node) if node.children[0].type in ("|", "&") else None
        if inner is None:
            return node
        node = inner


def _sole_child(node: Any) -> Any:
    """`node`'s one named child, comments aside; None for none or several."""
    named = [c for c in node.named_children if c.type not in _COMMENTS]
    return named[0] if len(named) == 1 else None


def _layout(node: Any, grammar: str) -> bool:
    """A comment or a Python `\\` line continuation, but a directive (`_directive`)."""
    return node.type in _LAYOUT and not _directive(node, grammar)


def _directive(comment: Any, grammar: str) -> bool:
    """A comment the toolchain reads: Go's `//go:noinline`, `//go:embed`, cgo's `//export`,
    a Python `# type:` comment."""
    if comment.type not in _COMMENTS:
        return False
    if grammar == "go":
        return bool(comment.text.startswith((b"//go:", b"//export ")))
    return grammar == "python" and comment.text[1:].lstrip().startswith(b"type:")


def _formatting(node: Any, grammar: str) -> bool:
    """A trailing comma, a JS / TS statement's `;` or an empty statement, or `_optional`:
    what a formatter adds or removes without changing the parse."""
    parent = node.parent
    if parent is None:
        return False
    if _optional(node, parent, grammar):
        return True
    if node.type == ",":
        return _trailing_comma(node, parent, grammar)
    if grammar not in _ECMA or node.type not in (";", "empty_statement"):
        return False
    if node.type == "empty_statement":
        return parent.type in _STATEMENT_LISTS and not _before_string(node)
    if parent.type == "empty_statement":
        return False
    if parent.type in _MEMBER_LISTS:
        return True
    up = parent.parent
    in_for_head = up is not None and up.type in _FORS
    in_for_head = in_for_head and not _same(up.child_by_field_name("body"), parent)
    return node.next_sibling is None and not in_for_head


def _string_statement(node: Any) -> bool:
    """`node` is a JS / TS expression statement of one string, as a directive is."""
    if node is None or node.type != "expression_statement":
        return False
    inner = _sole_child(node)
    return inner is not None and inner.type == "string"


def _before_string(empty: Any) -> bool:
    """A string statement follows `empty` before any other statement: dropping `empty`
    would make that string a directive (`; "use strict";`)."""
    node = empty.next_sibling
    while node is not None and node.type in {*_COMMENTS, "empty_statement"}:
        node = node.next_sibling
    return _string_statement(node)


def _optional(node: Any, parent: Any, grammar: str) -> bool:
    """A Python tuple's or `with`'s parentheses, a JS / TS type member's `,`, the empty
    `()` of `new A()`, Rust's comma after a match arm (every one is dropped: source that
    leaves out a required one does not parse)."""
    if grammar == "python":
        return node.type in ("(", ")") and parent.type in {*_BARE_TUPLES, "with_clause"}
    if grammar == "rust":
        return (node.type, parent.type) == (",", "match_arm")
    if grammar in _ECMA and node.type == "arguments":
        return parent.type == "new_expression" and not node.named_children
    return grammar in _ECMA and node.type == "," and parent.type in _TYPE_MEMBERS


def _trailing_comma(comma: Any, parent: Any, grammar: str) -> bool:
    """`comma` follows an element and closes a bracket, and is no tuple's only comma nor
    a macro's argument."""
    after, before = comma.next_sibling, comma.prev_sibling
    while after is not None and after.type in _COMMENTS:
        after = after.next_sibling
    while before is not None and before.type in _COMMENTS:
        before = before.prev_sibling
    if after is None or after.type not in _CLOSERS or before is None:
        return False
    if before.type in ("(", "[", "{", ","):
        return False
    if parent.type == "token_tree" or (grammar == "cpp" and parent.type == "argument_list"):
        return False
    return parent.type not in _TUPLES or sum(c.type == "," for c in parent.children) > 1


def _quote(token: Any, text: str, grammar: str) -> str:
    """A token's `text`, a string's quotes as `"`: a Python string's (its prefix lowercased
    without `u`) and a JS / TS string's; a template, a char literal or any other token as
    it is; a number's letters lowercased."""
    if token.type in _NUMBERS:
        return text.lower()
    if grammar == "python" and token.type in ("string_start", "string_end"):
        prefix = text.rstrip("'\"")
        quotes = text[len(prefix) :].replace("'", '"')
        return prefix.lower().replace("u", "") + quotes
    return '"' if _js_quote(token, grammar) else text


def _js_quote(token: Any, grammar: str) -> bool:
    """`token` is the `'` around a JS / TS string."""
    if token.type != "'" or grammar not in _ECMA:
        return False
    return token.parent is not None and token.parent.type == "string"


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
    if leaf.type in _SHORTHAND_BINDINGS:
        return "other"  # `const { NAME } = lib`, Rust's `let S { NAME } = s`
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
    if parent.type in ("expression_list", "tuple") and parent.parent is not None:
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
    return bool(leaf.type in _REFERENCES and (prev is None or prev.type not in _ACCESS))


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
