"""Resolve typed Python member reads in the scope that declares the type."""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from symtable import SymbolTable, symtable
from typing import Any

from server.code.domain.extract.edges_treesitter import Edge, _caller_spans, _CallResolver
from server.code.domain.extract.lang_python import (
    first_present_candidate,
    import_candidates,
    module_scope,
)
from server.code.domain.extract.py_members import KIND_CONST
from server.code.domain.extract.spans import last_span_at
from server.code.domain.extract.treesitter import ParsedFile, Symbol, file_symbol_id, node_text

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
READ_DEPENDENCY = "uses_type"


@dataclass(frozen=True)
class MemberRead:
    line: int
    receiver: str
    member: str
    receiver_type: str = ""
    imported: tuple[str, str] | None = None


def read_candidate_names(sites: list[MemberRead]) -> set[str]:
    """Names that need parser-preferred candidates for typed reads."""
    names = {site.member for site in sites}
    names.update(site.imported[1] for site in sites if site.imported)
    return names


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
        text = pf.source.decode("utf-8")
        module = symtable(text, pf.path, "exec")
        statements: list[ast.AST] = list(ast.parse(text).body)
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
    direct = _from_imports(statements)
    top_functions = {
        (node.name, node.lineno)
        for node in statements
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    imports.difference_update(_global_rebindings(children))
    sites: list[MemberRead] = []
    for line, receiver, member, owner in candidates:
        scope = scopes.get((owner.name, owner.line))
        if scope is None or receiver not in scope.get_identifiers():
            continue
        binding = scope.lookup(receiver)
        if binding.is_global() and receiver in imports and (origin := direct.get(receiver)):
            sites.append(MemberRead(line, receiver, member, imported=origin))
        elif (
            (owner.name, owner.line) in top_functions
            and binding.is_parameter()
            and not (binding.is_assigned() or binding.is_imported())
        ):
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
        annotation = parameter.child_by_field_name("type")
        if annotation is None or not (name_type := _simple_type(node_text(annotation, source))):
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


def _from_imports(nodes: list[ast.AST]) -> dict[str, tuple[str, str] | None]:
    imports: dict[str, tuple[str, str] | None] = {}
    for statement in nodes:
        if not isinstance(statement, ast.ImportFrom):
            continue
        module = "." * statement.level + (statement.module or "")
        for alias in statement.names:
            local = alias.asname or alias.name
            binding = (module, alias.name)
            if imports.setdefault(local, binding) != binding:
                imports[local] = None
    return imports


def _simple_type(text: str) -> str:
    name = text.strip().strip("'\"")
    return name if name.isidentifier() else ""


def _module_nodes(root: ast.Module) -> list[ast.AST]:
    nodes: list[ast.AST] = list(root.body)
    for node in nodes:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            nodes.extend(ast.iter_child_nodes(node))
    return nodes


@dataclass
class _ModuleTypes:
    path: str
    local: set[str]
    imports: dict[str, tuple[str, str] | None]
    annotations: dict[str, str]


def _module_types(path: str, source: str) -> _ModuleTypes | None:
    try:
        root = ast.parse(source)
    except (SyntaxError, RecursionError):
        return None
    nodes = _module_nodes(root)
    imports = _from_imports(nodes)
    if "*" in imports:
        return None
    # Assignments or competing definitions can change the meaning of a bare type.
    rebound = {
        node.id
        for node in nodes
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }
    rebound.update(
        node.name for node in nodes if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    rebound.update(
        alias.asname or alias.name.split(".", 1)[0]
        for node in nodes
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    classes = Counter(node.name for node in nodes if isinstance(node, ast.ClassDef))
    direct = {node.name for node in root.body if isinstance(node, ast.ClassDef)}
    local = {
        name
        for name in direct
        if name not in imports and name not in rebound and classes[name] == 1
    }
    imports = {
        name: origin
        for name, origin in imports.items()
        if name not in rebound and name not in classes
    }
    annotations = {
        node.target.id: _annotation_name(node.annotation)
        for node in root.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    return _ModuleTypes(path, local, imports, annotations)


def _annotation_name(annotation: ast.expr) -> str:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return _simple_type(annotation.value)
    return ""


def _dependency_paths(path: str, spec: str, context: dict[str, Any]) -> list[str]:
    present = context.get("py_present", set())
    roots = context.get("py_roots", ())
    if bound := first_present_candidate(path, spec, present, roots):
        return [bound]
    return import_candidates(path, spec, roots) if spec.startswith(".") else []


def unindexed_absolute_modules(
    pf: ParsedFile,
    sites: list[MemberRead],
    types: dict[tuple[str, str], tuple[str, str]],
    modules: dict[str, _ModuleTypes],
    context: dict[str, Any],
) -> set[str]:
    """Path candidates to check against Git before relying on the index's order."""
    imports = {(pf.path, site.imported[0]) for site in sites if site.imported}
    type_sites = {*types.values(), *((pf.path, s.receiver_type) for s in sites if s.receiver_type)}
    for path, name in type_sites:
        if (module := modules.get(path)) and (origin := module.imports.get(name)):
            imports.add((path, origin[0]))
    roots, present = context.get("py_roots", ()), context.get("py_present", set())
    return {
        candidate
        for path, spec in imports
        if not spec.startswith(".") and not first_present_candidate(path, spec, present, roots)
        for candidate in import_candidates(path, spec, roots)
    }


def _type_owner(
    module: _ModuleTypes,
    name: str,
    context: dict[str, Any],
    dependencies: set[str],
) -> str:
    if name in module.local:
        return f"{module_scope(module.path)}::{name}"
    if origin := module.imports.get(name):
        spec, declared = origin
        paths = _dependency_paths(module.path, spec, context)
        dependencies.update(paths)
        if len(paths) == 1 and paths[0] in context.get("py_present", set()):
            return f"{module_scope(paths[0])}::{declared}"
    return ""


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
    types: dict[tuple[str, str], tuple[str, str]],
    modules: dict[str, _ModuleTypes],
    lane_context: dict[str, Any] | None,
) -> list[Edge]:
    """Record binding reads and the member each declared type can resolve."""
    context = lane_context or {}
    functions = [s for s in pf.symbols if s.kind in {"function", "test"} and s.has_body]
    owner_at = last_span_at(_caller_spans(pf, functions))
    out: dict[tuple[str, str, int], Edge] = {}
    dependencies: set[str] = set()
    for site in sites:
        owner = owner_at(site.line)
        if owner is None:
            continue
        target = targets.get(site.receiver) if site.imported is not None else None
        if site.imported:
            dependencies.update(_dependency_paths(pf.path, site.imported[0], context))
        if target is not None:
            target_name, target_id = target
            out[(owner.id, target_id, site.line)] = Edge(
                "reads",
                owner.id,
                target_id,
                owner.qualified_name,
                target_name,
                pf.path,
                site.line,
                1.0,
            )
        declaration = (
            (*types[target], target[0].rsplit("::", 1)[-1])
            if target is not None and target in types
            else None
        )
        bound = _member_target(
            site, pf.path, declaration, name_table, modules, context, dependencies
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
    return [*out.values(), *_dependency_edges(pf.path, dependencies)]


def module_types(pf: ParsedFile, sources: dict[str, str]) -> dict[str, _ModuleTypes]:
    texts = {**sources, pf.path: pf.source.decode("utf-8")}
    return {path: module for path, text in texts.items() if (module := _module_types(path, text))}


def _member_target(
    site: MemberRead,
    path: str,
    declaration_type: tuple[str, str, str] | None,
    name_table: dict[str, list[tuple[str, str, str]]],
    modules: dict[str, _ModuleTypes],
    context: dict[str, Any],
    dependencies: set[str],
) -> tuple[str, str] | None:
    type_name = site.receiver_type
    if declaration_type is not None:
        path, type_name, declaration = declaration_type
        dependencies.add(path)
        if path not in modules or modules[path].annotations.get(declaration) != type_name:
            return None
    if not type_name or path not in modules:
        return None
    if not (type_owner := _type_owner(modules[path], type_name, context, dependencies)):
        return None
    qualified = f"{type_owner}::{site.member}"
    candidates = {sid for sid, name, _ in name_table.get(site.member, ()) if name == qualified}
    return (next(iter(candidates)), qualified) if len(candidates) == 1 else None


def _dependency_edges(path: str, dependencies: set[str]) -> list[Edge]:
    return [
        Edge(
            READ_DEPENDENCY,
            file_symbol_id(path),
            file_symbol_id(target),
            path,
            target,
            path,
            1,
            1.0,
        )
        for target in sorted(dependencies - {path})
    ]
