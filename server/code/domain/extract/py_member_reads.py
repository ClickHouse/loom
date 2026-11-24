"""Resolve Python member reads through the same receiver types as calls."""

from __future__ import annotations

from ast import ImportFrom
from ast import parse as parse_ast
from collections import defaultdict
from dataclasses import dataclass
from symtable import SymbolTable, symtable
from typing import Any

from server.code.domain.extract.edges_treesitter import Edge, _caller_spans, _CallResolver
from server.code.domain.extract.py_members import KIND_CONST
from server.code.domain.extract.py_receivers import type_name_of
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
    receiver_type: str = ""
    imported: tuple[str, str] | None = None


@dataclass(frozen=True)
class _FunctionScope:
    name: str
    line: int
    annotations: dict[str, str]


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
    children = _child_scopes(module)
    scopes = {
        (scope.get_name(), scope.get_lineno()): scope
        for scope in children
        if scope.get_type() == "function"
    }
    imports = {
        row.get_name()
        for row in module.get_symbols()
        if row.is_imported() and not row.is_assigned()
    }
    direct = _direct_imports(pf)
    imports.difference_update(_global_rebindings(children))
    sites: list[MemberRead] = []
    for line, receiver, member, owner in candidates:
        scope = scopes.get((owner.name, owner.line))
        if scope is None or receiver not in scope.get_identifiers():
            continue
        binding = scope.lookup(receiver)
        if binding.is_global() and receiver in imports and (origin := direct.get(receiver)):
            sites.append(MemberRead(line, receiver, member, imported=origin))
        elif binding.is_parameter() and not binding.is_assigned():
            if receiver_type := owner.annotations.get(receiver):
                sites.append(MemberRead(line, receiver, member, receiver_type))
    return sites


def _child_scopes(module: SymbolTable) -> list[SymbolTable]:
    todo = list(module.get_children())
    for scope in todo:
        todo.extend(scope.get_children())
    return todo


def _global_rebindings(children: list[SymbolTable]) -> set[str]:
    return {
        row.get_name()
        for scope in children
        for row in scope.get_symbols()
        if row.is_declared_global() and (row.is_assigned() or row.is_imported())
    }


def _annotations(parameters: Any, source: bytes) -> dict[str, str]:
    types: dict[str, str] = {}
    for parameter in parameters.named_children if parameters is not None else ():
        if not (name_type := type_name_of(parameter.child_by_field_name("type"), source)):
            continue
        name = parameter.child_by_field_name("name") or next(
            (child for child in parameter.named_children if child.type == "identifier"), None
        )
        if name is not None and name.type == "identifier":
            types[node_text(name, source)] = name_type
    return types


def _attribute_sites(pf: ParsedFile) -> list[tuple[int, str, str, _FunctionScope]]:
    sites: list[tuple[int, str, str, _FunctionScope]] = []
    stack: list[tuple[Any, _FunctionScope | None]] = [(pf.tree.root_node, None)]
    while stack:
        node, owner = stack.pop()
        if node.type == "function_definition":
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name is not None and body is not None:
                owner = _FunctionScope(
                    node_text(name, pf.source),
                    node.start_point[0] + 1,
                    _annotations(node.child_by_field_name("parameters"), pf.source),
                )
                stack.append((body, owner))
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
) -> _CallResolver:
    local: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in pf.symbols:
        if symbol.kind == "function":
            local[symbol.name].append(symbol)
    return _CallResolver(pf, name_table, local, lane_context=lane_context)


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
) -> dict[str, tuple[str, str]]:
    """Receiver name to the exact qualified name and id of its imported value."""
    imports = {site.receiver: site.imported for site in sites if site.imported}
    if not imports:
        return {}
    values = {n: [r for r in rows if r[2] == KIND_CONST] for n, rows in name_table.items()}
    resolver = _resolver(pf, values, lane_context)
    out: dict[str, tuple[str, str]] = {}
    for name, origin in sorted(imports.items()):
        if origin != resolver.py_bindings.get(name):
            continue
        bound = resolver.import_bound("", name, resolver.candidates(name, name))
        if isinstance(bound, tuple) and bound[2] == 1.0:
            out[name] = bound[1], bound[0]
    return out


def read_edges(
    pf: ParsedFile,
    sites: list[MemberRead],
    name_table: dict[str, list[tuple[str, str, str]]],
    targets: dict[str, tuple[str, str]],
    types: dict[tuple[str, str], str],
    lane_context: dict[str, Any] | None,
) -> list[Edge]:
    """Record binding reads and the member each declared type can resolve."""
    resolver = _resolver(pf, name_table, lane_context)
    functions = [s for s in pf.symbols if s.kind in {"function", "test"} and s.has_body]
    owner_at = last_span_at(_caller_spans(pf, functions))
    out: dict[tuple[str, str, int], Edge] = {}
    for site in sites:
        owner = owner_at(site.line)
        if owner is None:
            continue
        target = targets.get(site.receiver) if site.imported is not None else None
        if target is not None:
            target_name, target_id = target
            # A declaration can change even when both member definitions stay live.
            out[(owner.id, target_id, site.line)] = Edge(
                "reads", owner.id, target_id, owner.qualified_name, target_name,
                pf.path, site.line, 1.0,
            )
        receiver_type = site.receiver_type or (types.get(target) if target is not None else None)
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
