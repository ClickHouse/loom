"""SQL inside application code, any grammar.

`lang_py_islands` lifts the SQL a Python file runs; this module is the same contract
with the grammar as data, so Go (`db.QueryContext(ctx, "SELECT …")`, `sqlx.Get`, gorm
`Raw`) and Java (`prepareStatement("…")`, `jdbcTemplate.query("…")`, `@Query("…")`) read
through one walker:

  * an `IslandGrammar` names the call node, the string node types, the
    client method table (method → dialect), the object hints (`spark` →
    Spark SQL), the format methods whose first string argument is the SQL
    (`fmt.Sprintf`, `String.format`), the annotations that carry SQL
    (`@Query`, MyBatis `@Select`), and two grammar-specific readers: the
    callee's (method, object) names and a string node's (text,
    interpolated) value.
  * the walker applies the Python islands' rules: sinks first (the first
    positional argument that carries a string — Go's context-first
    signatures put the SQL second), then bare literals under the
    leading-keyword guard; holes (`%s`, `%d`, `?`, `:name`, a non-string
    `+` operand) become the placeholder at confidence 0.7; the statements,
    created objects, facets and column rows are the SQL lanes' own.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lang_py_islands import _client_arg_is_sql, _holes, _looks_like_sql
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_shell_islands import (
    PLACEHOLDER,
    Island,
    island_facets,
    island_statements,
)
from server.code.domain.extract.lang_sql import statement_symbol
from server.code.domain.extract.lang_sql_columns import (
    column_facets,
    column_lineage_on,
    column_symbols,
    file_columns,
)
from server.code.domain.extract.lang_sql_dialect import detect_dialect
from server.code.domain.extract.lang_sql_model import Facet, Header, Statement
from server.code.domain.extract.lang_sql_objects import object_symbols, reference_facets
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

_NAMED_PARAM_RE = re.compile(r"(?<![:\w]):[A-Za-z_]\w*\b|\$\d+\b")


@dataclass(frozen=True)
class IslandGrammar:
    lang: str
    call_node: str
    string_nodes: frozenset[str]
    methods: Mapping[str, str]
    callee: Callable[[Any, bytes], tuple[str, str]]
    string_value: Callable[[Any, bytes], tuple[str, bool]]
    objects: Mapping[str, str] = field(default_factory=dict)
    format_methods: frozenset[str] = frozenset()
    annotations: Mapping[str, str] = field(default_factory=dict)
    annotation_node: str = ""
    concat_node: str = "binary_expression"
    docstring_like: Callable[[Any], bool] = lambda _n: False
    # `X = "SELECT …"; X += " WHERE …"` — (node type, left field, right field) of a
    # plain assignment / declaration, and of the augmented one whose operator field is
    # `+=`
    assign_nodes: tuple[tuple[str, str, str], ...] = ()
    aug_assign_node: tuple[str, str, str, str] = ("", "", "", "")
    # `sb.append("SELECT ").append(" FROM t")` — a method whose receiver chain carries
    # string pieces; and the node type + field of a constructor call whose first string
    # argument starts the chain
    chain_methods: frozenset[str] = frozenset()
    chain_seed_node: str = ""


@dataclass
class CodeSqlUnits:
    islands: list[Island] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    facets: list[Facet] = field(default_factory=list)


def _string_parts(arg: Any, g: IslandGrammar, source: bytes) -> list[Any]:
    """The string node(s) an argument carries, None marking a hole: the
    literal, the first string argument of a format method's call, the
    operands of `+` in order (a non-string operand is a hole)."""
    if arg.type in g.string_nodes:
        return [arg]
    if arg.type == g.call_node:
        method, _obj = g.callee(arg, source)
        if method in g.format_methods:
            args = arg.child_by_field_name("arguments")
            for a in args.named_children if args is not None else []:
                parts = _string_parts(a, g, source)
                if parts:
                    return parts
        return []
    if arg.type == g.concat_node:
        left = arg.child_by_field_name("left")
        right = arg.child_by_field_name("right")
        op = arg.child_by_field_name("operator")
        op_text = (
            node_text(op, source)
            if op is not None
            else next((c.type for c in arg.children if not c.is_named), "")
        )
        if op_text != "+" or left is None or right is None:
            return []
        lp, rp = _string_parts(left, g, source), _string_parts(right, g, source)
        if not lp and not rp:
            return []
        return [*(lp or [None]), *(rp or [None])]
    if arg.type in ("parenthesized_expression", "expression_list") and len(arg.named_children) == 1:
        return _string_parts(arg.named_children[0], g, source)
    if g.chain_seed_node and arg.type == g.chain_seed_node:
        # `new StringBuilder("SELECT …")`: the first string argument seeds the chain
        args = arg.child_by_field_name("arguments")
        for a in args.named_children if args is not None else []:
            return _string_parts(a, g, source)
        return []
    if arg.type in ("list", "tuple", "array_initializer", "composite_literal"):
        # `"".join(["SELECT", " FROM t"])`, `{"a", "b"}` — the elements in order, a
        # non-string element a hole
        elems: list[Any] = []
        for e in arg.named_children:
            ep = _string_parts(e, g, source)
            elems.extend(ep or [None])
        return elems if any(p is not None for p in elems) else []
    return []


def chain_parts(call: Any, g: IslandGrammar, source: bytes) -> list[Any]:
    """The string pieces of an `.append("…").append("…")` chain, outermost call given,
    in source order; [] unless every link is a chain method or the seed constructor."""
    if not g.chain_methods:
        return []
    links: list[Any] = []
    cur = call
    while cur is not None and cur.type == g.call_node:
        method, _obj = g.callee(cur, source)
        if method not in g.chain_methods:
            return []
        links.append(cur)
        obj = cur.child_by_field_name("object")
        if obj is None:
            return []
        cur = obj
    if cur is None or (cur.type != g.chain_seed_node and cur.type != "identifier"):
        return []
    parts: list[Any] = []
    if cur.type == g.chain_seed_node:
        args = cur.child_by_field_name("arguments")
        for a in args.named_children if args is not None else []:
            parts.extend(_string_parts(a, g, source) or [None])
            break
    for link in reversed(links):
        args = link.child_by_field_name("arguments")
        first = next((a for a in (args.named_children if args is not None else [])), None)
        parts.extend(_string_parts(first, g, source) or [None] if first is not None else [])
    return parts if len([p for p in parts if p is not None]) >= 2 else []


def _leftmost(node: Any, g: IslandGrammar) -> Any:
    """The leftmost operand of a `+` chain (`((q + a) + b)` → `q`)."""
    cur = node
    while cur is not None and cur.type == g.concat_node:
        cur = cur.child_by_field_name("left")
    return cur


def _island(
    parts: list[Any], g: IslandGrammar, source: bytes, dialect: str, via: str, formatted: bool
) -> Island | None:
    real = [n for n in parts if n is not None]
    if not real:
        return None
    values = [g.string_value(n, source) if n is not None else (PLACEHOLDER, True) for n in parts]
    text = "".join(v[0] for v in values)
    interpolated = any(v[1] for v in values)
    text, holed = _holes(text)
    text, n_named = _NAMED_PARAM_RE.subn(PLACEHOLDER, text)
    if not text.strip():
        return None
    return Island(
        text,
        real[0].start_point[0] + 1,
        via,
        interpolated or holed or formatted or bool(n_named),
        (),
        dialect,
    )


def _scope_key(node: Any) -> int:
    """The nearest function-like ancestor's id (the file's root otherwise)."""
    cur = node.parent
    while cur is not None:
        if (
            "function" in cur.type
            or "method" in cur.type
            or cur.type in ("constructor_declaration", "lambda_expression", "func_literal")
        ):
            return int(cur.id)
        cur = cur.parent
    return 0


class _Chains:
    """Per-(scope, variable) string-piece chains built from assignments: `x = pieces`
    starts one, `x += pieces` and `x = x + pieces` extend it, and the number of
    statements that fed it decides whether it is "assembled" (two or more)."""

    def __init__(self, g: IslandGrammar, source: bytes) -> None:
        self.g, self.source = g, source
        self.chains: dict[tuple[int, str], list[Any]] = {}
        self.statements_in: dict[tuple[int, str], int] = {}
        self.order: list[tuple[int, str]] = []

    def name_of(self, node: Any) -> str:
        if node is None:
            return ""
        if node.type == "expression_list" and node.named_children:
            node = node.named_children[0]
        return node_text(node, self.source) if node.type == "identifier" else ""

    def plain_assign(self, n: Any) -> None:
        g = self.g
        for ntype, lf, rf in g.assign_nodes:
            if n.type != ntype:
                continue
            op = n.child_by_field_name("operator")
            if op is not None and node_text(op, self.source) != "=":
                return  # `+=` is the augmented form
            left, right = n.child_by_field_name(lf), n.child_by_field_name(rf)
            if right is not None and right.type == "expression_list" and right.named_children:
                right = right.named_children[0]
            var = self.name_of(left)
            if not var or right is None:
                return
            key = (_scope_key(n), var)
            if right.type == g.concat_node and key in self.chains:
                # `x = x + "…" + y` extends the chain
                if self.name_of(_leftmost(right, g)) == var:
                    parts = _string_parts(right, g, self.source)
                    if parts and parts[0] is None:
                        parts = parts[1:]  # the variable itself is not a hole
                    self._extend(key, parts)
                    return
            parts = _string_parts(right, g, self.source)
            self.chains[key] = list(parts) if parts else []
            self.statements_in[key] = 1
            if key not in self.order:
                self.order.append(key)
            return

    def augmented(self, n: Any) -> None:
        aug_type, aug_left, aug_op, aug_right = self.g.aug_assign_node
        if not aug_type or n.type != aug_type:
            return
        op = n.child_by_field_name(aug_op)
        var = self.name_of(n.child_by_field_name(aug_left))
        if op is None or node_text(op, self.source) != "+=" or not var:
            return
        key = (_scope_key(n), var)
        if key in self.chains:
            self._extend(key, _string_parts(n.child_by_field_name(aug_right), self.g, self.source))

    def _extend(self, key: tuple[int, str], parts: list[Any]) -> None:
        if parts:
            self.chains[key].extend(parts)
            self.statements_in[key] += 1

    def chained(self, n: Any) -> bool:
        """`sb.append("…").append(x)` as a statement extends `sb`'s chain; True when the
        node was such a chain (its inner calls are then not visited again)."""
        if not self.g.chain_methods or n.type != self.g.call_node:
            return False
        method, _obj = self.g.callee(n, self.source)
        if method not in self.g.chain_methods:
            return False
        cur = n
        links: list[Any] = []
        while cur is not None and cur.type == self.g.call_node:
            m, _o = self.g.callee(cur, self.source)
            if m not in self.g.chain_methods:
                return False
            links.append(cur)
            cur = cur.child_by_field_name("object")
        var = self.name_of(cur)
        if not var:
            return False
        key = (_scope_key(n), var)
        if key not in self.chains:
            return False
        parts: list[Any] = []
        for link in reversed(links):
            args = link.child_by_field_name("arguments")
            first = next((a for a in (args.named_children if args is not None else [])), None)
            if first is not None:
                parts.extend(_string_parts(first, self.g, self.source) or [None])
        self._extend(key, parts)
        return True

    def visit(self, n: Any) -> None:
        if self.chained(n):
            return
        self.plain_assign(n)
        self.augmented(n)
        for c in n.children:
            self.visit(c)


def assembled_islands(pf: ParsedFile, g: IslandGrammar, claimed: set[int]) -> list[Island]:
    """A statement built across assignments in one scope — `q := "SELECT a"; q += " FROM
    t"; q = q + " WHERE x"` — is one island anchored at the first assignment, with the
    pieces joined in source order and each non-string operand a hole. Only pieces from
    two or more statements whose joined head reads as SQL count (one assignment with a
    `+` chain is the literal lane's); the pieces are claimed so the literal lane does
    not count the first one alone."""
    if pf.tree is None or not g.assign_nodes:
        return []
    chains = _Chains(g, pf.source)
    chains.visit(pf.tree.root_node)
    out: list[Island] = []
    for key in chains.order:
        parts = chains.chains.get(key, [])
        real = [p for p in parts if p is not None]
        if chains.statements_in.get(key, 0) < 2 or len(real) < 2:
            continue
        if any(p.start_byte in claimed for p in real):
            continue
        isl = _island(parts, g, pf.source, "", "assembled", True)
        if isl is None or not _looks_like_sql(isl.text):
            continue
        claimed.update(p.start_byte for p in real)
        out.append(isl)
    return out


def _take(
    cand: Any,
    g: IslandGrammar,
    source: bytes,
    dialect: str,
    via: str,
    claimed: set[int],
    out: list[Island],
) -> bool:
    parts = _string_parts(cand, g, source)
    formatted = cand.type in (g.call_node, g.concat_node)
    isl = _island(parts, g, source, dialect, via, formatted)
    if isl is None or not _client_arg_is_sql(isl.text):
        return False
    claimed.update(p.start_byte for p in parts if p is not None)
    out.append(isl)
    return True


def _sink_islands(pf: ParsedFile, g: IslandGrammar, claimed: set[int]) -> list[Island]:
    """Client-call arguments (the first string-bearing positional one) and
    SQL-carrying annotation values."""
    source = pf.source
    out: list[Island] = []

    def visit(n: Any) -> None:
        if n.type == g.call_node:
            method, obj = g.callee(n, source)
            if method in g.methods:
                dialect = g.objects.get(obj) or g.methods[method]
                args = n.child_by_field_name("arguments")
                for a in args.named_children if args is not None else []:
                    if a.type != "comment" and _take(a, g, source, dialect, "call", claimed, out):
                        break
        elif g.annotation_node and n.type == g.annotation_node:
            name = n.child_by_field_name("name")
            ann = node_text(name, source).rsplit(".", 1)[-1] if name is not None else ""
            if ann in g.annotations:
                args = n.child_by_field_name("arguments")
                for a in args.named_children if args is not None else []:
                    value = a.child_by_field_name("value") if a.type == "element_value_pair" else a
                    if value is not None and _take(
                        value, g, source, g.annotations[ann], "annotation", claimed, out
                    ):
                        break
        for c in n.children:
            visit(c)

    if pf.tree is not None:
        visit(pf.tree.root_node)
    return out


def _literal_islands(pf: ParsedFile, g: IslandGrammar, claimed: set[int]) -> list[Island]:
    """Bare SQL-shaped literals and `+` chains nobody hands to a client."""
    source = pf.source
    out: list[Island] = []

    def literal_island(n: Any) -> None:
        parts = _string_parts(n, g, source)
        real = [p for p in parts if p is not None]
        if not real or any(p.start_byte in claimed for p in real):
            return
        if not _looks_like_sql(g.string_value(real[0], source)[0]):
            return
        isl = _island(parts, g, source, "", "literal", n.type == g.concat_node)
        if isl is not None:
            claimed.update(p.start_byte for p in real)
            out.append(isl)

    def visit(n: Any) -> None:
        if n.type == g.concat_node:
            literal_island(n)  # the chain is one island with holes for the non-strings
            return
        if n.type == g.call_node and g.chain_methods:
            parts = chain_parts(n, g, source)
            if parts:
                real = [p for p in parts if p is not None]
                if not any(p.start_byte in claimed for p in real) and _looks_like_sql(
                    g.string_value(real[0], source)[0]
                ):
                    isl = _island(parts, g, source, "", "assembled", True)
                    if isl is not None:
                        claimed.update(p.start_byte for p in real)
                        out.append(isl)
                        return
        if n.type in g.string_nodes and n.start_byte not in claimed and not g.docstring_like(n):
            literal_island(n)
            return
        for c in n.children:
            visit(c)

    if pf.tree is not None:
        visit(pf.tree.root_node)
    return out


def find_code_islands(pf: ParsedFile, g: IslandGrammar) -> list[Island]:
    """The SQL islands of one parsed file, in source order: client-call arguments and
    annotation values first, then statements assembled across assignments, then bare
    SQL-shaped literals."""
    if pf.tree is None:
        return []
    claimed: set[int] = set()
    out = _sink_islands(pf, g, claimed)
    out.extend(assembled_islands(pf, g, claimed))
    out.extend(_literal_islands(pf, g, claimed))
    out.sort(key=lambda i: i.line)
    return out


def settle_dialects(islands: list[Island], file_default: str = "") -> list[Island]:
    """The client's dialect, else the island's own text, else the file's one (or
    majority) client, else the file's import-derived default, else undecided."""
    if all(i.dialect for i in islands):
        return islands
    named = Counter(i.dialect for i in islands if i.dialect)
    if len(named) == 1:
        guess = next(iter(named))
    elif named:
        guess = named.most_common(1)[0][0]
    else:
        guess = detect_dialect("\n".join(i.text for i in islands))[0] or file_default
    return [
        Island(
            i.text,
            i.line,
            i.via,
            i.interpolated,
            i.settings,
            i.dialect or detect_dialect(i.text)[0] or guess,
        )
        for i in islands
    ]


def code_sql_units(pf: ParsedFile, g: IslandGrammar, file_default: str = "") -> CodeSqlUnits:
    """Statements, symbols (statement records, created objects, columns when column
    lineage is on) and facets for the SQL a file runs."""
    islands = settle_dialects(find_code_islands(pf, g), file_default)
    units = CodeSqlUnits(islands=islands)
    if not islands:
        return units
    pairs = island_statements(islands)
    statements = [st for st, _ in pairs]
    mod = module_scope(pf.path)
    units.statements = statements
    symbols = [statement_symbol(pf.path, mod, st) for st in statements]
    symbols.extend(object_symbols(pf.path, mod, statements))
    facets = island_facets(Header(), pairs, islands) + reference_facets(statements)
    if column_lineage_on():
        symbols.extend(column_symbols(pf.path, mod, statements))
        facets = facets + column_facets(statements, file_columns(statements))
    units.symbols = [s for s in symbols if PLACEHOLDER not in s.qualified_name]
    units.facets = [f for f in facets if f.name and PLACEHOLDER not in f.name]
    return units


__all__ = [
    "CodeSqlUnits",
    "IslandGrammar",
    "assembled_islands",
    "code_sql_units",
    "find_code_islands",
    "settle_dialects",
]
