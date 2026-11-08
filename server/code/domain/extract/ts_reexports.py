"""Named re-export chains followed to the declaration.

A barrel re-exports what it does not declare: `export * from "./constants"`,
`export { stringify } from "./utils/stringify"`, `export { default as
Root } from "./button.svelte"`, or `import Root from "./x"; export { Root as
Button }`. The import rung of the call ladder binds a name to the module the
specifier resolves to; when that module is a barrel the name is declared
elsewhere, and without this module the rung can only narrow to the barrel's
subtree at 0.9. This module traces each referenced symbol along its import
chain, following aliases, re-exports and barrel files until it reaches the
original declaration, over the texts the index holds, bounded and cached,
so the rung binds at 1.0 where the chain ends in a declaration.

Pure functions read one module's text; `resolve_export` walks the chain
through an async text reader the ingest supplies.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Awaitable, Callable

from server.code.domain.extract.lang_typescript_imports import import_candidates
from server.code.domain.extract.ts_config import strip_comments

MAX_DEPTH = 8
_DECL_RE = (
    r"^[^\S\n]*export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
    r"(?:abstract\s+class|class|function\*?|const|let|var|enum|interface|type|namespace)\s+{name}\b"
)
_DEFAULT_NAME_RE = re.compile(
    r"^[^\S\n]*export\s+default\s+(?:async\s+)?(?:function\*?|class)\s+([A-Za-z_$][\w$]*)", re.M
)
_DEFAULT_IDENT_RE = re.compile(
    r"^[^\S\n]*export\s+default\s+([A-Za-z_$][\w$]*)[^\S\n]*+;?[^\S\n]*+$", re.M
)
_DEFAULT_ANON_RE = re.compile(
    r"^[^\S\n]*export\s+default\s++(?:async\s++)?(?:function\*?|class)?\s*+[({<]", re.M
)
_EXPORT_LIST_RE = re.compile(r"""\bexport\s*\{([^{}]*)\}\s*(?:from\s*["']([^"']+)["'])?""")
_EXPORT_STAR_RE = re.compile(
    r"""\bexport\s*\*\s*(?:as\s+([A-Za-z_$][\w$]*)\s+)?from\s*["']([^"']+)["']"""
)
# A clause stops at the next `import` keyword: an import with no `from` would otherwise
# search on to the end of the file from each one, quadratic in a file of them.
_IMPORT_RE = re.compile(
    r"""\bimport\s++((?:(?!\bimport\b)[^;'"])+?)(?<=\S)\s+from\s*["']([^"']+)["']"""
)


def _parts(spec_list: str) -> list[tuple[str, str]]:
    """`A as B, type C, D` → [(A, B), (D, D)]; type-only entries skipped."""
    out: list[tuple[str, str]] = []
    for part in spec_list.split(","):
        part = part.strip()
        if not part or part.startswith("type "):
            continue
        if " as " in part:
            name, _, alias = (x.strip() for x in part.partition(" as "))
        else:
            name = alias = part
        ok_name = re.fullmatch(r"[A-Za-z_$][\w$]*|default", name)
        if ok_name and re.fullmatch(r"[A-Za-z_$][\w$]*", alias):
            out.append((name, alias))
    return out


def declared_name(text: str, name: str) -> str | None:
    """The name `name` is declared under in `text`, when the module declares
    (not re-exports) it: itself for a named declaration; for `default`, the
    declaration's own name (`export default function Foo` → Foo, `export
    default Bar` → Bar) or 'default' for an anonymous one. None when the
    module only re-exports or does not mention it."""
    clean = strip_comments(text)
    if name == "default":
        m = _DEFAULT_NAME_RE.search(clean)
        if m:
            return m.group(1)
        m = _DEFAULT_IDENT_RE.search(clean)
        if m:
            return m.group(1)
        return "default" if _DEFAULT_ANON_RE.search(clean) else None
    if re.search(_DECL_RE.format(name=re.escape(name)), clean, re.M):
        return name
    # `export { X }` / `export { Y as X }` with no `from`: a local declaration exported by list
    imported: set[str] | None = None  # read once: a scan per export list is quadratic
    for m in _EXPORT_LIST_RE.finditer(clean):
        if m.group(2):
            continue
        for local, alias in _parts(m.group(1)):
            if alias == name:
                imported = _imported_locals(clean) if imported is None else imported
                if local not in imported:
                    return local
    return None


def _imported_locals(clean: str) -> set[str]:
    """Every local name an import binds: a namespace, a default or a named import."""
    out: set[str] = set()
    for m in _IMPORT_RE.finditer(clean):
        clause = m.group(1)
        out.update(re.findall(r"(?:^|[\s,{])\*\s+as\s+([A-Za-z_$][\w$]*)", clause))
        dflt = re.match(r"^\s*([A-Za-z_$][\w$]*)\s*(?:,|$)", clause)
        if dflt:
            out.add(dflt.group(1))
        inner = re.search(r"\{([^{}]*)\}", clause)
        if inner:
            out.update(alias for _n, alias in _parts(inner.group(1)))
    return out


def reexport_steps(text: str, name: str) -> list[tuple[str, str]]:
    """Where `name` may come from when this module does not declare it:
    [(specifier, name in that module)] — a named re-export (`export { A as
    name } from`), an imported-then-exported alias (`import A from "x";
    export { A as name }`, `import { A } from "x"; export { A as name }`),
    then every `export * from` in order (with `name` unchanged)."""
    clean = strip_comments(text)
    out: list[tuple[str, str]] = []
    imports: dict[str, tuple[str, str]] = {}
    for m in _IMPORT_RE.finditer(clean):
        clause, spec = m.group(1), m.group(2)
        dflt = re.match(r"^\s*([A-Za-z_$][\w$]*)\s*(?:,|$)", clause)
        if dflt:
            imports[dflt.group(1)] = (spec, "default")
        inner = re.search(r"\{([^{}]*)\}", clause)
        if inner:
            for n, alias in _parts(inner.group(1)):
                imports[alias] = (spec, n)
    for m in _EXPORT_LIST_RE.finditer(clean):
        spec = m.group(2)
        for n, alias in _parts(m.group(1)):
            if alias != name:
                continue
            if spec:
                out.append((spec, n))
            elif n in imports:
                out.append(imports[n])
    for m in _EXPORT_STAR_RE.finditer(clean):
        if m.group(1):  # `export * as ns from` names a namespace, not our symbol
            continue
        out.append((m.group(2), name))
    return out


def resolve_spec(from_path: str, spec: str, present: set[str]) -> str | None:
    return next((c for c in import_candidates(from_path, spec) if c in present), None)


async def resolve_export(
    module_path: str, name: str, present: set[str],
    read_text: Callable[[str], Awaitable[str]], *, depth: int = 0,
    seen: set[tuple[str, str]] | None = None,
) -> tuple[str, str] | None:
    """(declaring path, declared name) for `name` exported from
    `module_path`, following re-exports and star exports depth-first up to
    `MAX_DEPTH`; None when no indexed module declares it (a library's, or a
    chain the index does not hold)."""
    seen = seen if seen is not None else set()
    if depth > MAX_DEPTH or (module_path, name) in seen:
        return None
    seen.add((module_path, name))
    if name == "default" and module_path.endswith((".svelte", ".astro", ".vue")):
        # a single-file component IS its module's default export
        from server.code.domain.extract.lang_sfc import component_unit_of

        return module_path, component_unit_of(module_path).split("::")[-1]
    text = await read_text(module_path)
    if not text:
        return None
    declared = declared_name(text, name)
    if declared is not None:
        return module_path, declared
    for spec, inner in reexport_steps(text, name):
        target = resolve_spec(module_path, spec, present)
        if target is None:
            continue
        hit = await resolve_export(target, inner, present, read_text, depth=depth + 1, seen=seen)
        if hit is not None:
            return hit
    return None


def is_barrel_like(path: str) -> bool:
    """A module the chain is worth following for: an index file, or any
    file — the caller decides by whether the direct module declared the
    name; this only names the cheap first check."""
    return posixpath.basename(path).split(".", 1)[0] == "index"


__all__ = [
    "MAX_DEPTH", "declared_name", "is_barrel_like", "reexport_steps", "resolve_export",
    "resolve_spec",
]
