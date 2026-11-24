"""Resolve Python member reads through the same receiver types as calls."""

from __future__ import annotations

from ast import ImportFrom
from ast import parse as parse_ast
from collections import defaultdict
from dataclasses import dataclass
from symtable import SymbolTable, symtable
from typing import Any

from server.code.domain.extract.edges_treesitter import Edge, _caller_spans, _CallResolver
from server.code.domain.extract.py_receivers import NAME_PREFIX
from server.code.domain.extract.spans import last_span_at
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

_GROUPING = frozenset(
    {
        "parenthesized_expression",
        "pattern_list",
        "list_pattern",
        "tuple_pattern",
        "list",
        "tuple",
    }
)
_WRITE_TARGETS = frozenset({"assignment", "augmented_assignment", "for_statement", "for_in_clause"})


@dataclass(frozen=True)
class MemberRead:
    line: int
    receiver: str
    member: str
    imported: bool


def _is_read(node: Any) -> bool:
    parent = node.parent
    while parent is not None and parent.type in _GROUPING:
        node, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type == "call" and parent.child_by_field_name("function") == node:
        return False
    if parent.type in _WRITE_TARGETS and parent.child_by_field_name("left") == node:
        return False
    return parent.type not in {"delete_statement", "as_pattern_target", "type"}


def read_sites(pf: ParsedFile) -> list[MemberRead]:
    """Attribute reads through a bare receiver. Calls and assignment targets have
    their own meanings and do not enter this list."""
    if pf.lang != "python" or pf.tree is None:
        return []
    candidates = _attribute_sites(pf)
    if not candidates:
        return []
    try:
        module = symtable(pf.source.decode("utf-8"), pf.path, "exec")
    except (SyntaxError, UnicodeDecodeError, RecursionError):
        return []
    scopes = _function_scopes(module)
    imports = {
        row.get_name()
        for row in module.get_symbols()
        if row.is_imported() and not row.is_assigned()
    }
    sites: list[MemberRead] = []
    for line, receiver, member, owner in candidates:
        scope = scopes.get(owner)
        if scope is None or receiver not in scope.get_identifiers():
            continue
        binding = scope.lookup(receiver)
        if binding.is_global() and not binding.is_assigned() and receiver in imports:
            sites.append(MemberRead(line, receiver, member, True))
        elif binding.is_local():
            sites.append(MemberRead(line, receiver, member, False))
    return sites


def _function_scopes(module: SymbolTable) -> dict[tuple[str, int], SymbolTable]:
    scopes: dict[tuple[str, int], SymbolTable] = {}
    todo = list(module.get_children())
    while todo:
        scope = todo.pop()
        todo.extend(scope.get_children())
        if scope.get_type() == "function":
            scopes[(scope.get_name(), scope.get_lineno())] = scope
    return scopes


def _attribute_sites(pf: ParsedFile) -> list[tuple[int, str, str, tuple[str, int]]]:
    sites: list[tuple[int, str, str, tuple[str, int]]] = []
    stack: list[tuple[Any, tuple[str, int] | None]] = [(pf.tree.root_node, None)]
    while stack:
        node, owner = stack.pop()
        if node.type == "function_definition":
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name is not None and body is not None:
                stack.append((body, (node_text(name, pf.source), node.start_point[0] + 1)))
            continue
        if node.type in {
            "class_definition",
            "lambda",
            "list_comprehension",
            "set_comprehension",
            "dictionary_comprehension",
            "generator_expression",
        }:
            owner = None
        stack.extend((child, owner) for child in reversed(node.named_children))
        if owner is None or node.type != "attribute" or not _is_read(node):
            continue
        receiver = node.child_by_field_name("object")
        member = node.child_by_field_name("attribute")
        if receiver is not None and receiver.type == "identifier" and member is not None:
            sites.append(
                (
                    node.start_point[0] + 1,
                    node_text(receiver, pf.source),
                    node_text(member, pf.source),
                    owner,
                )
            )
    return sites


def _resolver(
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
    lane_context: dict[str, Any] | None,
    types: dict[str, str] | None = None,
) -> _CallResolver:
    local: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in pf.symbols:
        if symbol.kind == "function":
            local[symbol.name].append(symbol)
    return _CallResolver(pf, name_table, local, types, lane_context)


def _direct_imports(pf: ParsedFile) -> dict[str, tuple[str, str] | None]:
    imports: dict[str, tuple[str, str] | None] = {}
    for node in pf.tree.root_node.named_children:
        if node.type != "import_from_statement":
            continue
        try:
            statement = parse_ast(node_text(node, pf.source)).body[0]
        except SyntaxError:
            continue
        if not isinstance(statement, ImportFrom):
            continue
        module = "." * statement.level + (statement.module or "")
        for alias in statement.names:
            local = alias.asname or alias.name
            binding = (module, alias.name)
            if imports.setdefault(local, binding) != binding:
                imports[local] = None
    return imports


def receiver_targets(
    pf: ParsedFile,
    sites: list[MemberRead],
    name_table: dict[str, list[tuple[str, str, str]]],
    lane_context: dict[str, Any] | None,
) -> dict[str, str]:
    """Receiver name to imported declaration. An import that can be shadowed
    supplies no type for a read."""
    names = {site.receiver for site in sites if site.imported}
    if not names:
        return {}
    resolver = _resolver(pf, name_table, lane_context)
    direct = _direct_imports(pf)
    out: dict[str, str] = {}
    for name in sorted(names):
        if direct.get(name) is None or direct[name] != resolver.py_bindings.get(name):
            continue
        bound = resolver.import_bound("", name, resolver.candidates(name, name))
        if isinstance(bound, tuple):
            out[NAME_PREFIX + name] = bound[1]
    return out


def read_edges(
    pf: ParsedFile,
    sites: list[MemberRead],
    name_table: dict[str, list[tuple[str, str, str]]],
    types: dict[str, str],
    lane_context: dict[str, Any] | None,
) -> list[Edge]:
    """Emit a read only when the receiver has a type and the member binds through it."""
    resolver = _resolver(pf, name_table, lane_context, types)
    functions = [s for s in pf.symbols if s.kind in {"function", "test"} and s.has_body]
    owner_at = last_span_at(_caller_spans(pf, functions))
    out: dict[tuple[str, str, int], Edge] = {}
    for site in sites:
        owner = owner_at(site.line)
        if owner is None:
            continue
        receiver_type = resolver.typer.type_of(
            site.line, site.receiver, include_imported=site.imported
        )
        if not receiver_type:
            continue
        bound = resolver._named_type_target(
            receiver_type,
            site.member,
            resolver.candidates(site.member, site.member),
            site.receiver,
        )
        if bound is None:
            continue
        target_id, target_name = bound
        out[(owner.id, target_id, site.line)] = Edge(
            "reads",
            owner.id,
            target_id,
            owner.qualified_name,
            target_name,
            pf.path,
            site.line,
            0.8,
        )
    return list(out.values())
