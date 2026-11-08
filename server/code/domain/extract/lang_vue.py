"""Vue single-file components.

A `.vue` file is three blocks at column 0 — `<template>`, `<script>` / `<script setup>`
and `<style>` — and no generic detector knows the extension, so without this module a
Vue UI indexes nothing. What a reader wants from a component is what the TypeScript lane
gives a module: the functions its script declares (at their own lines), the modules it
imports (as include edges), and the components it renders (as `calls` edges to the child
component, the way the TypeScript lane binds a JSX element), plus the component itself
as a unit other components call.

The script block is laid OFFSET-PRESERVINGLY into a blank text — the
block's lines at their own bytes, every other byte a space (a byte-exact
form of the `lang_hosts.synthetic_shell` precedent) — and parsed once with the grammar
its `lang` attribute names (`ts` / `tsx` → TypeScript, else JavaScript).
The returned `ParsedFile` carries the ORIGINAL bytes as its source, the
padded parse as its tree and the tag `typescript` / `javascript`: the
stamped chunker and edge walker know those tags and need no change; the
`.vue` extension tells a reader what the file is.

The component is one `function` symbol named after the file (`Foo.vue` → `Foo`),
spanning the `<template>` block: the template's reference sites attribute to it, and
another component's `<Foo>` binds to it through the import that names the file (the
import-bound rule). It does not span the whole file — the chunker's covered-lines guard
would then swallow the script's functions.

Template reference sites are read from the template text: a capitalised
element (`<DataView>`, `<Radix.Arrow>` with the member root riding in the
kind) is a `jsx` site exactly as `lang_typescript_refs.reference_sites` emits
one; a kebab-case element whose PascalCase form the script imports
(`<data-view>` for `import DataView from …`) is the same site; an event
handler (`@click="reload"`, `v-on:input="onInput(x)"`) or a text
interpolation call (`{{ format(row) }}`) is a `handler` site that binds
only to a same-file function. A capitalised element the script does NOT
import is a globally registered library component and yields nothing — a
repository-unique `Button` is never it.

Outside the extractor stamp with the other lane parsers: a namespace gains the rows by a
targeted backfill over its `.vue` tree, never by a heal.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any

from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_typescript import LANG_JS, LANG_TS, parse_typescript_text
from server.code.domain.extract.lang_typescript_imports import import_bindings
from server.code.domain.extract.lang_typescript_refs import reference_edges
from server.code.domain.extract.treesitter import ParsedFile, Symbol

KIND_COMPONENT_SIGNATURE = "component"
SITE_JSX = "jsx"
SITE_HANDLER = "handler"

_OPEN_RE = re.compile(r"^<(template|script|style)\b([^>]*)>", re.M)
_LANG_ATTR_RE = re.compile(r"""\blang\s*=\s*["']?([A-Za-z]+)""")
_TAG_RE = re.compile(r"<([A-Z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*)(?=[\s/>])")
_KEBAB_TAG_RE = re.compile(r"<([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?=[\s/>])")
# `@click="reload"`, `@click="reload(x)"`, `v-on:input="onInput"`; the value's
# first token must be the whole expression or a call of it — `router.push(…)`,
# `() => f()` and `emit('x')` are not a same-file function by name
_HANDLER_RE = re.compile(
    r"""(?<![\w.:-])(?:@|v-on:)[\w.:-]++\s*=\s*["']\s*([A-Za-z_$][\w$]*)\s*(?:\(|["'])"""
)
_INTERP_RE = re.compile(r"\{\{\s*([A-Za-z_$][\w$]*)\s*\(")
# A bound prop whose value is a bare identifier (`:formatter="fmtRow"`,
# `v-bind:on-save="save"`) passes a function down; the binder keeps it only when a
# same-file function of that name exists, so a bound ref costs nothing
_BOUND_PROP_RE = re.compile(
    r"""(?<![\w@])(?::|v-bind:)[\w.-]+\s*=\s*["']\s*([A-Za-z_$][\w$]*)\s*["']"""
)
_MACROS = ("defineProps", "defineEmits", "defineModel", "defineExpose")
_TEMPLATE_COMMENT_RE = re.compile(r"^\s*+<!--(.*?)-->", re.S)  # its text is stripped after
_MAX_DOC = 400


@dataclass(frozen=True)
class SfcBlock:
    """One top-level block: its kind, the opener's attributes, the lines of
    the opener and closer (1-indexed) and of the body between them
    (`body_start > body_end` for an empty block)."""

    kind: str
    attrs: str
    start_line: int
    end_line: int

    @property
    def body_start(self) -> int:
        return self.start_line + 1

    @property
    def body_end(self) -> int:
        return self.end_line - 1

    @property
    def lang(self) -> str:
        m = _LANG_ATTR_RE.search(self.attrs)
        return m.group(1).lower() if m else ""


def sfc_blocks(text: str) -> list[SfcBlock]:
    """The column-0 `<template>` / `<script>` / `<style>` blocks of a
    single-file component, in source order. A nested `<template #slot>` is
    indented and never at column 0; the closer is the first column-0
    `</kind>` after the opener. An opener without a closer runs to the end."""
    lines = text.split("\n")
    out: list[SfcBlock] = []
    i = 0
    while i < len(lines):
        m = _OPEN_RE.match(lines[i])
        if m is None:
            i += 1
            continue
        kind, attrs = m.group(1), m.group(2)
        closer = f"</{kind}>"
        if (
            lines[i].rstrip().endswith(closer)
            and lines[i].count(closer) >= 1
            and (lines[i].find(closer) > m.end() - 1)
        ):
            # `<script setup>import x from "y"</script>` on one line: no body lines
            out.append(SfcBlock(kind, attrs, i + 1, i + 1))
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].startswith(closer):
            j += 1
        out.append(SfcBlock(kind, attrs, i + 1, min(j, len(lines) - 1) + 1))
        i = j + 1
    return out


def script_grammar(blocks: list[SfcBlock]) -> tuple[str, str]:
    """(language tag, grammar) for the script blocks: `lang="ts"` →
    TypeScript, `lang="tsx"` → the tsx grammar, anything else JavaScript."""
    for b in blocks:
        if b.kind != "script":
            continue
        lang = b.lang
        if lang == "tsx":
            return LANG_TS, "tsx"
        if lang == "ts":
            return LANG_TS, "typescript"
    return LANG_JS, "javascript"


def synthetic_script(source: bytes, blocks: list[SfcBlock]) -> bytes:
    """The script blocks' body lines at their own bytes, every other byte a
    space (newlines kept) — one parse of it reports the component's lines
    AND byte offsets, so every tree reader (the edge walker, the chunker's
    unit lookup, `import_bindings`) can read the node text off the ORIGINAL
    bytes. A line-preserving text alone would shift the offsets."""
    rows = source.split(b"\n")
    out: list[bytes] = []
    keep: set[int] = set()
    for b in blocks:
        if b.kind == "script":
            keep.update(range(b.body_start, b.body_end + 1))
    for i, row in enumerate(rows, start=1):
        out.append(row if i in keep else b" " * len(row))
    return b"\n".join(out)


def component_name(path: str) -> str:
    """`components/common/DataView.vue` → `DataView`; an `index.vue` takes
    its directory's name."""
    stem = posixpath.basename(path)
    stem = stem[: stem.rfind(".")] if "." in stem else stem
    if stem.lower() == "index":
        parent = posixpath.basename(posixpath.dirname(path))
        return parent or stem
    return stem


def _template_doc(lines: list[str], block: SfcBlock) -> str:
    """A leading `<!-- … -->` comment in the template body, as the
    component's doc comment; '' when the body opens with markup."""
    head = "\n".join(lines[block.body_start - 1 : min(block.body_end, block.body_start + 4)])
    m = _TEMPLATE_COMMENT_RE.match(head)
    return m.group(1).strip()[:_MAX_DOC] if m else ""


def parse_vue(path: str, source: bytes) -> ParsedFile:
    """Parse one single-file component: the script block's symbols and
    import specifiers through the TypeScript lane, the component as a
    `function` unit over the template block."""
    text = source.decode("utf-8", "replace")
    lines = text.split("\n")
    blocks = sfc_blocks(text)
    lang, grammar = script_grammar(blocks)
    pf = parse_typescript_text(path, synthetic_script(source, blocks), lang, grammar)
    pf.source = source  # the tree's offsets are the original's (see `synthetic_script`)
    template = next((b for b in blocks if b.kind == "template"), None)
    name = component_name(path)
    if template is not None:
        pf.symbols.insert(
            0,
            Symbol(
                qualified_name=f"{module_scope(path)}::{name}",
                name=name,
                kind="function",
                signature=KIND_COMPONENT_SIGNATURE,
                path=path,
                start_line=template.start_line,
                end_line=template.end_line,
                doc_comment=_template_doc(lines, template),
                has_body=True,
            ),
        )
    pf.symbols.extend(macro_symbols(pf, name))
    return pf


def macro_symbols(pf: ParsedFile, component: str) -> list[Symbol]:
    """The component's interface as symbols — one `interface` per compiler macro call
    (`defineProps`, `defineEmits`, `defineModel`, `defineExpose`), qualified
    `<mod>::<Component>.props` / `.emits` / `.model` / `.expose`, spanning the call, its
    signature the member names the call declares (object keys, array strings, or the
    type literal's property names). A reader's `code.outline` of a component then shows
    its contract; `code.symbol Comp.props` opens it."""
    if pf.tree is None:
        return []
    source = pf.source
    out: list[Symbol] = []
    mod = module_scope(pf.path)

    def text(n: Any) -> str:
        return source[n.start_byte : n.end_byte].decode("utf-8", "replace")

    def member_names(call: Any) -> list[str]:
        names: list[str] = []
        args = call.child_by_field_name("arguments")
        targs = call.child_by_field_name("type_arguments")
        nodes = list(args.named_children) if args is not None else []
        if targs is not None:
            nodes.extend(targs.named_children)
        stack = nodes
        while stack:
            n = stack.pop(0)
            if n.type == "pair":
                k = n.child_by_field_name("key")
                if k is not None:
                    names.append(text(k).strip("'\"`"))
            elif n.type in ("string", "template_string"):
                names.append(text(n).strip("'\"`"))
            elif n.type == "property_signature":
                k = n.child_by_field_name("name")
                if k is not None:
                    names.append(text(k))
            elif n.type in (
                "object",
                "array",
                "object_type",
                "type_arguments",
                "arguments",
                "as_expression",
                "satisfies_expression",
                "parenthesized_expression",
            ):
                stack[:0] = list(n.named_children)
        return names

    def visit(n: Any) -> None:
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            callee = text(fn) if fn is not None else ""
            if callee in _MACROS:
                member = callee[len("define") :].lower()  # props | emits | model | expose
                names = member_names(n)
                sig = f"{member}: {', '.join(names)}" if names else member
                out.append(
                    Symbol(
                        qualified_name=f"{mod}::{component}.{member}",
                        name=f"{component}.{member}",
                        kind="interface",
                        signature=sig,
                        path=pf.path,
                        start_line=n.start_point[0] + 1,
                        end_line=n.end_point[0] + 1,
                        has_body=True,
                    )
                )
                return
        for c in n.children:
            visit(c)

    visit(pf.tree.root_node)
    return out


def template_block(pf: ParsedFile) -> SfcBlock | None:
    text = pf.source.decode("utf-8", "replace")
    return next((b for b in sfc_blocks(text) if b.kind == "template"), None)


def _pascal(kebab: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in kebab.split("-"))


def template_sites(
    pf: ParsedFile,
    bindings: dict[str, str] | None = None,
) -> list[tuple[int, str, str]]:
    """(1-indexed line, name, kind) for every reference the template makes:
    capitalised elements as `jsx` / `jsx:<root>` sites, kebab-case elements
    whose PascalCase form the script imports as `jsx` sites, event handlers
    and interpolation calls as `handler` sites; in source order."""
    block = template_block(pf)
    if block is None or block.body_start > block.body_end:
        return []
    bindings = import_bindings(pf) if bindings is None else bindings
    lines = pf.source.decode("utf-8", "replace").split("\n")
    body = "\n".join(lines[block.body_start - 1 : block.body_end])
    base = block.body_start
    out: list[tuple[int, str, str]] = []

    def line_of(offset: int) -> int:
        return base + body.count("\n", 0, offset)

    for m in _TAG_RE.finditer(body):
        text = m.group(1)
        bare = text.split(".")[-1]
        root = text.split(".")[0] if "." in text else ""
        out.append((line_of(m.start()), bare, f"{SITE_JSX}:{root}" if root else SITE_JSX))
    for m in _KEBAB_TAG_RE.finditer(body):
        name = _pascal(m.group(1))
        if name in bindings:
            out.append((line_of(m.start()), name, SITE_JSX))
    for rx in (_HANDLER_RE, _INTERP_RE):
        for m in rx.finditer(body):
            out.append((line_of(m.start(1)), m.group(1), SITE_HANDLER))
    # a bound prop is a site only when the value names a same-file function:
    # `:formatter="fmtRow"` passes one down, `:items="rows"` passes data and is not a
    # reference the graph records
    functions = {
        s.name
        for s in pf.symbols
        if s.kind == "function" and s.signature != KIND_COMPONENT_SIGNATURE
    }
    for m in _BOUND_PROP_RE.finditer(body):
        if m.group(1) in functions:
            out.append((line_of(m.start(1)), m.group(1), SITE_HANDLER))
    out.sort(key=lambda t: t[0])
    return out


def template_edges(
    pf: ParsedFile,
    sites: list[tuple[int, str, str]],
    name_table: dict[str, list[tuple[str, str, str]]],
    bindings: dict[str, str] | None = None,
) -> list[Edge]:
    """`calls` edges from the component for its template sites: an element
    the script imports binds through `lang_typescript_refs.reference_edges`
    (the definition at the module the import names, at 1.0; a library
    import yields nothing); an element it does not import is a globally
    registered library component and yields nothing; a handler binds to
    the same-file unique `function` of that name at 1.0, else nothing."""
    bindings = import_bindings(pf) if bindings is None else bindings
    bound = [
        s
        for s in sites
        if s[2].startswith(SITE_JSX)
        and ((s[2].split(":", 1)[1] if ":" in s[2] else s[1]) in bindings)
    ]
    edges = reference_edges(pf, bound, name_table, bindings) if bound else []
    component = next((s for s in pf.symbols if s.signature == KIND_COMPONENT_SIGNATURE), None)
    if component is None:
        return edges
    local: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        if s.kind == "function" and s is not component:
            local.setdefault(s.name, []).append(s)
    seen = {(e.edge_kind, e.from_symbol_id, e.to_symbol_id, e.line) for e in edges}
    for line, name, kind in sites:
        if kind != SITE_HANDLER or not (component.start_line <= line <= component.end_line):
            continue
        mine = local.get(name, [])
        if len(mine) != 1:
            continue
        key = ("calls", component.id, mine[0].id, line)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            Edge(
                "calls",
                component.id,
                mine[0].id,
                component.qualified_name,
                mine[0].qualified_name,
                pf.path,
                line,
                1.0,
            )
        )
    return edges


def is_vue_path(path: str) -> bool:
    return path.lower().endswith(".vue")


__all__ = [
    "KIND_COMPONENT_SIGNATURE",
    "SITE_HANDLER",
    "SITE_JSX",
    "SfcBlock",
    "component_name",
    "is_vue_path",
    "macro_symbols",
    "parse_vue",
    "script_grammar",
    "sfc_blocks",
    "synthetic_script",
    "template_block",
    "template_edges",
    "template_sites",
]
