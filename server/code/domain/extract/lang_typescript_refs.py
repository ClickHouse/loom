"""TypeScript / JavaScript reference sites and edges: type, value and member reads, bound
to their declaring file through the file's imports."""

from __future__ import annotations

from typing import Any

from server.code.domain.extract.edges_treesitter import REPO_WIDE_CONFIDENCE, Edge
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_typescript import (
    KIND_CONST,
    KIND_ENUM,
    KIND_FIELD,
    KIND_INTERFACE,
    KIND_MODULE,
    KIND_TYPE,
)
from server.code.domain.extract.lang_typescript_imports import import_candidates
from server.code.domain.extract.treesitter import ParsedFile, Symbol, file_unit, node_text

_TYPE_NOISE: frozenset[str] = frozenset(
    {
        # the language's and the runtime's utility and global types
        "Array",
        "Promise",
        "Partial",
        "Record",
        "Pick",
        "Omit",
        "Readonly",
        "Required",
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "Date",
        "Error",
        "Awaited",
        "ReturnType",
        "Parameters",
        "Exclude",
        "Extract",
        "NonNullable",
        "InstanceType",
        "ConstructorParameters",
        "ThisType",
        "Uppercase",
        "Lowercase",
        "Capitalize",
        "Uncapitalize",
        "Function",
        "Object",
        "String",
        "Number",
        "Boolean",
        "Symbol",
        "BigInt",
        "Uint8Array",
        "ArrayBuffer",
        "Buffer",
        "Response",
        "Request",
        "URL",
        "URLSearchParams",
        "Headers",
        "Iterable",
        "AsyncIterable",
        "Iterator",
        "AsyncIterator",
        "Generator",
        "AsyncGenerator",
        "PromiseLike",
        "ArrayLike",
        "Element",
        "Node",
        "Window",
        "Document",
        "Event",
        "MouseEvent",
        "KeyboardEvent",
        "HTMLElement",
        "HTMLInputElement",
        "HTMLDivElement",
        "HTMLButtonElement",
        "HTMLTextAreaElement",
        "HTMLFormElement",
        "HTMLSpanElement",
        "HTMLTableElement",
        "RegExp",
        "Intl",
        "JSON",
        # React's
        "ReactNode",
        "ReactElement",
        "FC",
        "FunctionComponent",
        "ComponentProps",
        "ComponentPropsWithoutRef",
        "ComponentType",
        "PropsWithChildren",
        "Dispatch",
        "SetStateAction",
        "MutableRefObject",
        "RefObject",
        "Ref",
        "CSSProperties",
        "SyntheticEvent",
        "ChangeEvent",
        "FormEvent",
        "ReactPortal",
        "Key",
        "JSX",
    }
)
_TYPE_DECLARING_PARENTS: frozenset[str] = frozenset(
    {
        "interface_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "type_parameter",
        "nested_type_identifier",
        "internal_module",
        "module",
    }
)
_JSX_ELEMENT_TYPES: frozenset[str] = frozenset({"jsx_opening_element", "jsx_self_closing_element"})
_TYPE_TARGET_KINDS: frozenset[str] = frozenset({KIND_INTERFACE, KIND_TYPE, "class", KIND_ENUM})
_JSX_TARGET_KINDS: frozenset[str] = frozenset({"function", KIND_CONST, "class"})


_MEMBER_CONTAINER_KINDS: frozenset[str] = frozenset({KIND_ENUM, KIND_CONST, KIND_MODULE, "class"})
_NAMESPACE_READ_KINDS: frozenset[str] = frozenset({KIND_CONST, KIND_FIELD, KIND_ENUM, "class"})


def _is_field(parent: Any, parent_type: str, field: str, node: Any) -> bool:
    if parent is None or parent.type != parent_type:
        return False
    f = parent.child_by_field_name(field)
    return f is not None and (f.start_byte, f.end_byte) == (node.start_byte, node.end_byte)


def member_read_sites(pf: ParsedFile, keep: set[str] | None = None) -> list[tuple[int, str, str]]:
    """(1-indexed line, object, member) for every `Obj.member` read whose object is a bare
    identifier the file imports or declares (`keep`), not the callee of a call and not the left side
    of an assignment. `this.x`, `a.b.c`'s outer step and `req.body` are not sites."""
    out: list[tuple[int, str, str]] = []
    if pf.tree is None:
        return out
    source = pf.source

    def walk(node: Any) -> None:
        for child in node.named_children:
            if child.type == "member_expression":
                obj = child.child_by_field_name("object")
                prop = child.child_by_field_name("property")
                parent = child.parent
                # node objects are not identity-stable: compare by span
                callee = _is_field(parent, "call_expression", "function", child)
                assigned = _is_field(parent, "assignment_expression", "left", child)
                if (
                    obj is not None
                    and prop is not None
                    and obj.type == "identifier"
                    and prop.type == "property_identifier"
                    and not callee
                    and not assigned
                ):
                    name = node_text(obj, source)
                    if keep is None or name in keep:
                        out.append((child.start_point[0] + 1, name, node_text(prop, source)))
            walk(child)

    walk(pf.tree.root_node)
    return out


_VALUE_PARENTS: frozenset[str] = frozenset(
    {
        "arguments",
        "pair",
        "jsx_expression",
        "array",
        "return_statement",
        "spread_element",
        "ternary_expression",
        "variable_declarator",
        "assignment_expression",
        "parenthesized_expression",
        "await_expression",
        "shorthand_property_identifier",
        "as_expression",
        "satisfies_expression",
        "non_null_expression",
        "type_assertion",
    }
)
_VALUE_TARGET_KINDS: frozenset[str] = frozenset(
    {"function", "class", KIND_CONST, KIND_ENUM, KIND_FIELD}
)


def value_reference_sites(pf: ParsedFile, keep: set[str] | None = None) -> list[tuple[int, str]]:
    """(1-indexed line, name) for every identifier used as a VALUE: an argument (`app.use(cors)`),
    an object property value or shorthand (`{ onError: fn }`, `{ handler }`), a JSX expression
    (`onClick={fn}`), an array element (`providers: [Cls]`), a returned, spread, aliased or assigned
    name. Not a callee, not the name a declaration introduces, not an object's member step, not a
    type. `keep` narrows to names the file declares or imports."""
    out: list[tuple[int, str]] = []
    if pf.tree is None:
        return out
    source = pf.source

    def _owning_field(parent: Any, field: str, node: Any) -> bool:
        f = parent.child_by_field_name(field)
        return f is not None and (f.start_byte, f.end_byte) == (node.start_byte, node.end_byte)

    def walk(node: Any) -> None:
        for child in node.named_children:
            t = child.type
            if t == "shorthand_property_identifier":
                name = node_text(child, source)
                if keep is None or name in keep:
                    out.append((child.start_point[0] + 1, name))
            elif t == "identifier":
                parent = child.parent
                pt = parent.type if parent is not None else ""
                name = node_text(child, source)
                if (
                    pt in _VALUE_PARENTS
                    and (keep is None or name in keep)
                    and not (
                        (pt == "variable_declarator" and _owning_field(parent, "name", child))
                        or (pt == "assignment_expression" and _owning_field(parent, "left", child))
                        or (pt == "pair" and _owning_field(parent, "key", child))
                    )
                ):
                    out.append((child.start_point[0] + 1, name))
            walk(child)

    walk(pf.tree.root_node)
    return out


def value_reference_edges(
    pf: ParsedFile,
    sites: list[tuple[int, str]],
    name_table: dict[str, list[tuple[str, str, str]]],
    bindings: dict[str, str] | None = None,
    chains: dict[str, tuple[str, str]] | None = None,
) -> list[Edge]:
    """`reads` edges for value references: the name binds on the reference ladder —
    same-file unique definition 1.0, import-bound 1.0, re-export chain,
    repository-unique `REPO_WIDE_CONFIDENCE` — to a function, class, const, enum or
    field row; a library import or an unknown name binds nothing. Passing a function is
    a read of its binding: the compiler tier records the same occurrence as a use."""
    local: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        local.setdefault(s.name, []).append(s)
    bindings = bindings or {}
    edges: list[Edge] = []
    seen: set[tuple[str, str, int]] = set()
    for line, name in sites:
        src = _enclosing_unit(pf, line) or file_unit(pf)
        if src is None:
            continue
        mine = [
            s
            for s in local.get(name, [])
            if s.kind in _VALUE_TARGET_KINDS and s.qualified_name != src.qualified_name
        ]
        if len(mine) == 1:
            to_id, to_qn, conf = mine[0].id, mine[0].qualified_name, 1.0
        elif mine:
            continue  # several same-file namesakes: nothing to pin the reference
        else:
            cands = [c for c in name_table.get(name, []) if c[2] in _VALUE_TARGET_KINDS]
            spec = bindings.get(name, "")
            bound = _import_bound(pf.path, spec, cands) if spec else None
            if bound is None and spec and chains and name in chains:
                bound = _chain_bound(name, chains[name], cands, name_table, _VALUE_TARGET_KINDS)
            if bound is not None:
                to_id, to_qn, conf = bound
            elif spec and not _is_barrel_spec(pf.path, spec, cands):
                continue  # a library's value
            elif len({c[1] for c in cands}) != 1:
                continue
            else:
                to_id, to_qn, conf = cands[0][0], cands[0][1], REPO_WIDE_CONFIDENCE
        if to_qn == src.qualified_name or (src.id, to_id, line) in seen:
            continue
        seen.add((src.id, to_id, line))
        edges.append(Edge("reads", src.id, to_id, src.qualified_name, to_qn, pf.path, line, conf))
    return edges


def member_read_edges(
    pf: ParsedFile,
    sites: list[tuple[int, str, str]],
    name_table: dict[str, list[tuple[str, str, str]]],
    bindings: dict[str, str] | None = None,
    chains: dict[str, tuple[str, str]] | None = None,
) -> list[Edge]:
    """`reads` edges for `Obj.member` sites: the object binds on the reference ladder
    (same-file unique container 1.0, import-bound 1.0, re-export chain,
    repository-unique `REPO_WIDE_CONFIDENCE`), then the member is the `field` row
    `<object>::<member>`; no row, no edge."""
    local: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        local.setdefault(s.name, []).append(s)
    bindings = bindings or {}
    edges: list[Edge] = []
    seen: set[tuple[str, str, int]] = set()
    for line, obj, member in sites:
        src = _enclosing_unit(pf, line) or file_unit(pf)
        if src is None:
            continue
        bound = _bind_container(pf, obj, local, name_table, bindings, chains)
        if bound is None:
            # `utils.VERSION` through `import * as utils` — the module is the container;
            # the member is its export at that module
            spec = bindings.get(obj, "")
            cands = [c for c in name_table.get(member, []) if c[2] in _NAMESPACE_READ_KINDS]
            via_ns = _import_bound(pf.path, spec, cands) if spec and obj not in local else None
            if via_ns is None:
                continue
            to_id, target, conf = via_ns
        else:
            qn, conf = bound
            target = f"{qn}::{member}"
            to_id = next((s.id for s in local.get(member, []) if s.qualified_name == target), "")
            if not to_id:
                to_id = next(
                    (
                        c[0]
                        for c in name_table.get(member, [])
                        if c[1] == target and c[2] == KIND_FIELD
                    ),
                    "",
                )
        if not to_id or target == src.qualified_name or (src.id, to_id, line) in seen:
            continue
        seen.add((src.id, to_id, line))
        edges.append(Edge("reads", src.id, to_id, src.qualified_name, target, pf.path, line, conf))
    return edges


def _bind_container(
    pf: ParsedFile,
    obj: str,
    local: dict[str, list[Symbol]],
    name_table: dict[str, list[tuple[str, str, str]]],
    bindings: dict[str, str],
    chains: dict[str, tuple[str, str]] | None,
) -> tuple[str, float] | None:
    mine = [s for s in local.get(obj, []) if s.kind in _MEMBER_CONTAINER_KINDS]
    if len(mine) == 1:
        return mine[0].qualified_name, 1.0
    cands = [c for c in name_table.get(obj, []) if c[2] in _MEMBER_CONTAINER_KINDS]
    spec = bindings.get(obj, "")
    bound = _import_bound(pf.path, spec, cands) if spec else None
    if bound is None and spec and chains and obj in chains:
        bound = _chain_bound(obj, chains[obj], cands, name_table, _MEMBER_CONTAINER_KINDS)
    if bound is not None:
        return bound[1], bound[2]
    if spec and not _is_barrel_spec(pf.path, spec, cands):
        return None  # a library object, or a module holding no such container
    if len({c[1] for c in cands}) != 1:
        return None
    return cands[0][1], REPO_WIDE_CONFIDENCE


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    """(1-indexed line, name, 'type' | 'jsx') for every type written in a
    type position (not the name a declaration introduces, not a namespace
    member, not a one-letter type parameter) and every capitalised JSX
    element name, in source order."""
    out: list[tuple[int, str, str]] = []
    if pf.tree is None:
        return out
    source = pf.source

    def walk(node: Any) -> None:
        for child in node.named_children:
            t = child.type
            if t == "type_identifier":
                parent = child.parent.type if child.parent is not None else ""
                name = node_text(child, source)
                declaring = parent in _TYPE_DECLARING_PARENTS
                if not declaring and len(name) > 1 and name not in _TYPE_NOISE:
                    out.append((child.start_point[0] + 1, name, "type"))
            elif t == "type_query":
                # `typeof Button` in a type position: a uses_type of the VALUE, never a
                # call
                ident = next((c for c in child.named_children if c.type == "identifier"), None)
                if ident is not None:
                    out.append((child.start_point[0] + 1, node_text(ident, source), "typeof"))
            elif t in _JSX_ELEMENT_TYPES:
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    text = node_text(name_node, source)
                    bare = text.split(".")[-1]
                    if bare[:1].isupper():
                        # `<Radix.Arrow>`: the member's root rides in the kind so the
                        # edge binds through the root's import, never to a repo namesake
                        root = text.split(".")[0] if "." in text else ""
                        site_kind = f"jsx:{root}" if root else "jsx"
                        out.append((child.start_point[0] + 1, bare, site_kind))
            walk(child)

    walk(pf.tree.root_node)
    return out


def _enclosing_unit(pf: ParsedFile, line: int) -> Symbol | None:
    """The innermost symbol whose span holds the line — a function first,
    else the container (an interface's field type is the interface's)."""
    best: Symbol | None = None
    for s in pf.symbols:
        if s.start_line <= line <= s.end_line and (
            best is None or (s.end_line - s.start_line) < (best.end_line - best.start_line)
        ):
            best = s
    return best


def _import_bound(
    from_path: str,
    spec: str,
    cands: list[tuple[str, str, str]],
) -> tuple[str, str, float] | None:
    """The candidate the import names — the one whose module is a path the specifier can
    resolve to (`./Cell` from `src/components/Grid/Grid.tsx` →
    `src.components.Grid.Cell`), at 1.0. None when no candidate lives there (a library
    import, or a barrel the re-export table resolves elsewhere: the caller decides the
    fallback)."""
    modules = {module_scope(c) for c in import_candidates(from_path, spec)}
    picked = {(c[0], c[1]) for c in cands if c[1].split("::", 1)[0] in modules}
    if len({qn for _sid, qn in picked}) != 1:
        return None
    sid, qn = sorted(picked)[0]
    return sid, qn, 1.0


def under_barrel(
    from_path: str,
    spec: str,
    cands: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """The candidates whose module lies under the directory a barrel specifier names
    (`../src` → `packages/client-node/src/index.ts` → every
    `packages.client-node.src.*`): a barrel re-exports what its directory defines, so an
    import through it narrows a name to that subtree even when the re-export table is
    not read."""
    dirs = {
        module_scope(c).rsplit(".index", 1)[0]
        for c in import_candidates(from_path, spec)
        if c.rsplit("/", 1)[-1].startswith("index.")
    }
    return [c for c in cands if any(c[1].split("::", 1)[0].startswith(d + ".") for d in dirs)]


def _is_barrel_spec(from_path: str, spec: str, cands: list[tuple[str, str, str]]) -> bool:
    """Could the specifier be a barrel (`./components` → `…/index.ts`) whose
    re-export the candidates' modules sit behind? True when a candidate's
    module lies under the specifier's directory."""
    dirs = {
        module_scope(c).rsplit(".index", 1)[0]
        for c in import_candidates(from_path, spec)
        if c.rsplit("/", 1)[-1].startswith("index.")
    }
    return any(c[1].split("::", 1)[0].startswith(d + ".") for d in dirs for c in cands)


def _chain_bound(
    name: str,
    chain: tuple[str, str],
    cands: list[tuple[str, str, str]],
    name_table: dict[str, list[tuple[str, str, str]]],
    kinds: frozenset[str],
) -> tuple[str, str, float] | None:
    """The barrel's re-export chain named the declaring module; the one definition there
    under the declared name binds at the import rung's grade."""
    module, declared = chain
    pool = cands if declared == name else [c for c in name_table.get(declared, []) if c[2] in kinds]
    hits = {
        (c[0], c[1])
        for c in pool
        if c[1].split("::", 1)[0] == module and c[1].split("::")[-1] == declared
    }
    if len(hits) != 1:
        return None
    cid, cqn = next(iter(hits))
    return cid, cqn, 1.0


def reference_edges(
    pf: ParsedFile,
    sites: list[tuple[int, str, str]],
    name_table: dict[str, list[tuple[str, str, str]]],
    bindings: dict[str, str] | None = None,
    chains: dict[str, tuple[str, str]] | None = None,
) -> list[Edge]:
    """`uses_type` / `calls` edges for the reference sites: same-file unique definition
    of the right kind at 1.0, else — for a name the file imports (`bindings`: local name
    → specifier) — the definition at the module the import names at 1.0, else the
    repository-unique one at `REPO_WIDE_CONFIDENCE`; nothing is recorded for a name that
    stays ambiguous or unknown (a library type, an imported component). An import-bound
    name that resolves to no indexed module is a library name and never falls back to
    the repository-unique rung (a repo-unique `Button` is not the `Button` imported from
    a UI package) — unless the import is a barrel (`…/index`), whose re-exports are not
    in the table."""
    local: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        local.setdefault(s.name, []).append(s)
    bindings = bindings or {}
    edges: list[Edge] = []
    seen: set[tuple[str, str, str, int]] = set()
    for line, name, kind in sites:
        kinds = _TYPE_TARGET_KINDS if kind == "type" else _JSX_TARGET_KINDS
        edge_kind = "uses_type" if kind in ("type", "typeof") else "calls"
        src = _enclosing_unit(pf, line) or file_unit(pf)
        if src is None:
            continue
        root = kind.split(":", 1)[1] if kind.startswith("jsx:") else ""
        if root:
            # a member element binds only through its root's import — a
            # namespace import of an indexed module; `<RadixTooltip.Arrow>`
            # is a library's Arrow, whatever the repository calls its own
            cands = [c for c in name_table.get(name, []) if c[2] in kinds]
            spec = bindings.get(root, "")
            bound = _import_bound(pf.path, spec, cands) if spec else None
            if bound is None:
                continue
            to_id, to_qn, conf = bound
            mine = []
        else:
            mine = [
                s
                for s in local.get(name, [])
                if s.kind in kinds and s.qualified_name != src.qualified_name
            ]
        if root:
            pass
        elif len(mine) == 1:
            to_id, to_qn, conf = mine[0].id, mine[0].qualified_name, 1.0
        else:
            cands = [c for c in name_table.get(name, []) if c[2] in kinds]
            spec = bindings.get(name, "")
            bound = _import_bound(pf.path, spec, cands) if spec else None
            if bound is None and spec and chains and name in chains:
                bound = _chain_bound(name, chains[name], cands, name_table, kinds)
            if bound is not None:
                to_id, to_qn, conf = bound
            elif spec and not _is_barrel_spec(pf.path, spec, cands):
                continue  # imported from a library, or from a module holding no such definition
            elif len({c[1] for c in cands}) != 1:
                continue
            else:
                to_id, to_qn, conf = cands[0][0], cands[0][1], REPO_WIDE_CONFIDENCE
        if to_qn == src.qualified_name:
            continue
        key = (edge_kind, src.id, to_id, line)
        if key in seen:
            continue
        seen.add(key)
        edges.append(Edge(edge_kind, src.id, to_id, src.qualified_name, to_qn, pf.path, line, conf))
    return edges
