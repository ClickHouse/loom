"""Svelte and Astro components: the Vue lane's single-file-component machinery for two
more markup languages.

A `.svelte` file is column-0 `<script>` / `<style>` blocks and, around
them, the component's markup; there is no `<template>` wrapper. An
`.astro` file is a frontmatter fence — TypeScript between two column-0
`---` lines — and the markup below it. In both, what a reader wants is
what the Vue lane gives: the functions the script declares at their own
lines, the modules it imports as include edges, the component itself as a
`function` unit spanning its markup, and the components it renders and
the handlers it wires as `calls` edges.

The script is laid offset-preservingly into a blank text and parsed with
the TypeScript grammar (`lang="ts"` in Svelte; Astro frontmatter is always
TypeScript), exactly as `lang_vue.synthetic_script` does, so the tree's
offsets index the ORIGINAL bytes. Markup sites: a capitalised element
(`<Button>`, `<Dialog.Root>`) is a `jsx` site; `on:click={save}`,
`onclick={save}` (Svelte 5), `onClick={save}` and a function-valued prop
(`<Child onSave={save}>`) are `handler` sites that bind only to a same-file
function; an expression call (`{format(row)}`, `{#if check(x)}`, `{@html
render(x)}`) is a `handler` site too. Edges come from
`lang_vue.template_edges`, unchanged.

Like the Vue lane, outside the extractor stamp: a namespace gains the
rows by a targeted backfill over its `.svelte` / `.astro` tree.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_typescript import LANG_TS, parse_typescript_text
from server.code.domain.extract.lang_typescript_imports import import_bindings, import_candidates
from server.code.domain.extract.lang_typescript_refs import _enclosing_unit
from server.code.domain.extract.lang_vue import (
    _TAG_RE,
    KIND_COMPONENT_SIGNATURE,
    SITE_HANDLER,
    SITE_JSX,
    SfcBlock,
    component_name,
    script_grammar,
    sfc_blocks,
    synthetic_script,
)
from server.code.domain.extract.text_lines import line_at, newline_offsets
from server.code.domain.extract.treesitter import ParsedFile, Symbol
from server.code.domain.extract.ts_config import strip_comments

_FENCE = "---"
# `on:click={save}`, `on:click|preventDefault={save}`, `onclick={save}` (Svelte 5),
# `onClick={save}` (Astro client islands): the braces hold a bare name or a call of it
# Not after a `|`: a search from each `on` of a modifier chain would rescan the chain.
_EVENT_RE = re.compile(
    r"(?<![\w|])on:?[A-Za-z]++(?:\|[A-Za-z]++)*+\s*=\s*\{\s*([A-Za-z_$][\w$]*)\s*[(}]"
)
# any `{ … name( … }` expression: `{format(row)}`, `{#if check(x)}`, `{@html render(x)}`,
# `{#each sort(items) as item}` — the first identifier that is called
_EXPR_CALL_RE = re.compile(r"\{(?:[#:@/]\w+\s++)?[^{}\n]*?(?<![\w$])([A-Za-z_$][\w$]*+)\s*\(")
# a prop whose value is a bare identifier: `<Child onSave={save}>`, `formatter={fmt}`
_PROP_RE = re.compile(r"""(?<![\w:@-])([A-Za-z][\w:-]*+)\s*=\s*\{\s*([A-Za-z_$][\w$]*)\s*\}""")
_HTML_COMMENT_RE = re.compile(r"^\s*+<!--(.*?)-->", re.S)  # its text is stripped after
_MAX_DOC = 400
_SFC_EXTS = (".svelte", ".astro", ".vue")
_BARREL_NAMES = ("index.ts", "index.js", "index.mts", "index.mjs")
# `import Root, { x } from "./button.svelte"` — the default import's local name and its spec
_DEFAULT_IMPORT_RE = re.compile(
    r"""\bimport\s+([A-Za-z_$][\w$]*)(?:\s*,\s*\{[^{}]*\})?\s+from\s+["']([^"']+)["']"""
)
_NAMED_IMPORT_RE = re.compile(
    r"""\bimport\s+(?:[A-Za-z_$][\w$]*\s*,\s*)?\{([^{}]*)\}\s+from\s+["']([^"']+)["']"""
)
_EXPORT_LIST_RE = re.compile(r"""\bexport\s+\{([^{}]*)\}(?:\s+from\s+["']([^"']+)["'])?""")


def _alias_parts(part: str) -> tuple[str, str] | None:
    """`A as B` → (A, B); `A` → (A, A); a `type` entry or junk → None."""
    part = part.strip()
    if not part or part.startswith("type "):
        return None  # a type re-export is not a component
    if " as " in part:
        name, _, alias = (x.strip() for x in part.partition(" as "))
    else:
        name = alias = part
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", name) or not re.fullmatch(r"[A-Za-z_$][\w$]*", alias):
        return None
    return name, alias


def default_import_bindings(pf: ParsedFile) -> dict[str, str]:
    """{local name: specifier} for the DEFAULT imports of the file — the form
    a component is imported by (`import BlockViewerCode from
    './block-viewer-code.svelte'`), whose local name is the importer's
    choice and need not match the module's unit name."""
    text = pf.source.decode("utf-8", "replace")
    return {
        m.group(1): m.group(2) for m in _DEFAULT_IMPORT_RE.finditer(text) if m.group(1) != "type"
    }


def barrel_alias_map(text: str) -> dict[str, tuple[str, str]]:
    """{exported name: (specifier, imported name | 'default')} for a barrel that imports
    and re-exports under other names — shadcn-svelte's `import Root from
    "./button.svelte"; export { Root, Root as Button }` — plus the direct `export {
    default as Root, A as B } from "./x"` forms. A name re-exported through `export *
    from` is not here (the barrel edges handle it)."""
    text = strip_comments(text)  # one pass: the pattern rescans from every unclosed `/*`
    local: dict[str, tuple[str, str]] = {}
    for m in _DEFAULT_IMPORT_RE.finditer(text):
        if m.group(1) != "type":
            local[m.group(1)] = (m.group(2), "default")
    for m in _NAMED_IMPORT_RE.finditer(text):
        for part in m.group(1).split(","):
            pair = _alias_parts(part)
            if pair is not None:
                local[pair[1]] = (m.group(2), pair[0])
    out: dict[str, tuple[str, str]] = {}
    for m in _EXPORT_LIST_RE.finditer(text):
        spec = m.group(2)
        for part in m.group(1).split(","):
            pair = _alias_parts(part)
            if pair is None:
                continue
            name, exported = pair
            if spec:
                out[exported] = (spec, name)
            elif name in local:
                out[exported] = local[name]
    return out


def _resolve_spec(from_path: str, spec: str, present: set[str]) -> str | None:
    return next((c for c in import_candidates(from_path, spec) if c in present), None)


def component_unit_of(path: str) -> str:
    """The qualified name of a single-file component's unit."""
    if path.endswith(".vue"):
        return f"{module_scope(path)}::{component_name(path)}"
    return f"{module_scope(path)}::{sfc_component_name(path)}"


async def import_targets(
    pf: ParsedFile,
    present: set[str],
    read_text: Any,
) -> dict[str, str]:
    """{local name: qualified unit} for every element name the file imports whose target
    is a single-file component reached by a default import (`import BlockViewerCode from
    './block-viewer-code.svelte'` → that file's unit, however the importer named it) or
    through a barrel's alias (`import { Button } from '$lib/components/ui/button'` → the
    barrel's `Root as Button` → `./button.svelte`'s unit). `read_text(path)` returns a
    live file's text; only barrels are read, at most once each."""
    out: dict[str, str] = {}
    text = pf.source.decode("utf-8", "replace")
    for local, spec in default_import_bindings(pf).items():
        target = _resolve_spec(pf.path, spec, present)
        if target is not None and target.endswith(_SFC_EXTS):
            out[local] = component_unit_of(target)
    barrels: dict[str, dict[str, tuple[str, str]]] = {}
    for m in _NAMED_IMPORT_RE.finditer(text):
        spec = m.group(2)
        barrel = _resolve_spec(pf.path, spec, present)
        if barrel is None or posixpath.basename(barrel) not in _BARREL_NAMES:
            continue
        if barrel not in barrels:
            barrels[barrel] = barrel_alias_map(await read_text(barrel) or "")
        amap = barrels[barrel]
        for part in m.group(1).split(","):
            pair = _alias_parts(part)
            if pair is None:
                continue
            imported, local = pair
            hit = amap.get(imported)
            if hit is None:
                continue
            inner_spec, inner_name = hit
            target = _resolve_spec(barrel, inner_spec, present)
            if target is not None and target.endswith(_SFC_EXTS) and inner_name == "default":
                out[local] = component_unit_of(target)
    return out


def import_target_edges(
    pf: ParsedFile,
    sites: list[tuple[int, str, str]],
    targets: dict[str, str],
    name_table: dict[str, list[tuple[str, str, str]]],
) -> list[Edge]:
    """`calls` edges at 1.0 from the enclosing unit of each `jsx` site whose
    element name `targets` maps to a component unit the table holds."""
    by_qn = {c[1]: c[0] for cands in name_table.values() for c in cands}
    edges: list[Edge] = []
    seen: set[tuple[str, str, int]] = set()
    for line, name, kind in sites:
        if kind != SITE_JSX or name not in targets:
            continue
        to_qn = targets[name]
        to_id = by_qn.get(to_qn)
        src = _enclosing_unit(pf, line)
        if to_id is None or src is None or src.qualified_name == to_qn:
            continue
        key = (src.id, to_id, line)
        if key in seen:
            continue
        seen.add(key)
        edges.append(Edge("calls", src.id, to_id, src.qualified_name, to_qn, pf.path, line, 1.0))
    return edges


def is_svelte_path(path: str) -> bool:
    return path.lower().endswith(".svelte")


def is_astro_path(path: str) -> bool:
    return path.lower().endswith(".astro")


def is_sfc_path(path: str) -> bool:
    return is_svelte_path(path) or is_astro_path(path)


def sfc_component_name(path: str) -> str:
    """`Button.svelte` → `Button`; SvelteKit's `+page.svelte` / `+layout.svelte`
    take their route directory: `routes/blog/+page.svelte` → `blog_page`."""
    stem = posixpath.basename(path)
    stem = stem[: stem.rfind(".")] if "." in stem else stem
    if stem.startswith("+"):
        route = posixpath.basename(posixpath.dirname(path)) or "root"
        return f"{re.sub(r'[^A-Za-z0-9_]', '_', route)}_{stem[1:]}"
    return component_name(path)


def astro_blocks(text: str) -> list[SfcBlock]:
    """The frontmatter as one `script` block (`lang="ts"`): the lines between
    the first two column-0 `---` fences. No fence: no script."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return []
    for j in range(1, len(lines)):
        if lines[j].strip() == _FENCE:
            return [SfcBlock("script", ' lang="ts"', 1, j + 1)]
    return [SfcBlock("script", ' lang="ts"', 1, len(lines))]


def markup_range(lines: list[str], blocks: list[SfcBlock]) -> tuple[int, int] | None:
    """(first, last) 1-indexed lines of markup: every non-blank line outside
    the script / style blocks; None when the file is script only."""
    covered: set[int] = set()
    for b in blocks:
        covered.update(range(b.start_line, b.end_line + 1))
    marks = [i for i, row in enumerate(lines, start=1) if i not in covered and row.strip()]
    return (marks[0], marks[-1]) if marks else None


def _markup_doc(lines: list[str], start: int) -> str:
    head = "\n".join(lines[start - 1 : start + 4])
    m = _HTML_COMMENT_RE.match(head)
    return m.group(1).strip()[:_MAX_DOC] if m else ""


def _parse_sfc(
    path: str,
    source: bytes,
    blocks: list[SfcBlock],
    lang: str,
    grammar: str,
) -> ParsedFile:
    text = source.decode("utf-8", "replace")
    lines = text.split("\n")
    pf = parse_typescript_text(path, synthetic_script(source, blocks), lang, grammar)
    pf.source = source  # the tree's offsets are the original's
    span = markup_range(lines, blocks)
    if span is not None:
        name = sfc_component_name(path)
        pf.symbols.insert(
            0,
            Symbol(
                qualified_name=f"{module_scope(path)}::{name}",
                name=name,
                kind="function",
                signature=KIND_COMPONENT_SIGNATURE,
                path=path,
                start_line=span[0],
                end_line=span[1],
                doc_comment=_markup_doc(lines, span[0]),
                has_body=True,
            ),
        )
    return pf


def parse_svelte(path: str, source: bytes) -> ParsedFile:
    """A Svelte component: its `<script>` blocks (instance and `module`)
    through the TypeScript lane, the component as a unit over its markup."""
    blocks = sfc_blocks(source.decode("utf-8", "replace"))
    lang, grammar = script_grammar(blocks)
    return _parse_sfc(path, source, blocks, lang, grammar)


def parse_astro(path: str, source: bytes) -> ParsedFile:
    """An Astro component: its frontmatter through the TypeScript grammar,
    the component as a unit over the markup below the fence."""
    blocks = astro_blocks(source.decode("utf-8", "replace"))
    return _parse_sfc(path, source, blocks, LANG_TS, "typescript")


def markup_sites(
    pf: ParsedFile,
    bindings: dict[str, str] | None = None,
) -> list[tuple[int, str, str]]:
    """(1-indexed line, name, kind) for every reference the markup makes:
    capitalised elements as `jsx` / `jsx:<root>` sites; event handlers,
    function-valued props and expression calls as `handler` sites (bound
    only to a same-file function by `lang_vue.template_edges`)."""
    text = pf.source.decode("utf-8", "replace")
    lines = text.split("\n")
    blocks = astro_blocks(text) if is_astro_path(pf.path) else sfc_blocks(text)
    span = markup_range(lines, blocks)
    if span is None:
        return []
    covered: set[int] = set()
    for b in blocks:
        covered.update(range(b.start_line, b.end_line + 1))
    body = "\n".join(row if i not in covered else "" for i, row in enumerate(lines, start=1))
    bindings = import_bindings(pf) if bindings is None else bindings

    newlines = newline_offsets(body)

    out: list[tuple[int, str, str]] = []
    for m in _TAG_RE.finditer(body):
        tag = m.group(1)
        bare, root = tag.split(".")[-1], (tag.split(".")[0] if "." in tag else "")
        out.append((line_at(newlines, m.start()), bare, f"{SITE_JSX}:{root}" if root else SITE_JSX))
    for m in _EVENT_RE.finditer(body):
        out.append((line_at(newlines, m.start(1)), m.group(1), SITE_HANDLER))
    functions = {
        s.name
        for s in pf.symbols
        if s.kind == "function" and s.signature != KIND_COMPONENT_SIGNATURE
    }
    for m in _PROP_RE.finditer(body):
        if not m.group(1).startswith("on") and m.group(2) in functions:
            out.append((line_at(newlines, m.start(2)), m.group(2), SITE_HANDLER))
    for m in _EXPR_CALL_RE.finditer(body):
        if m.group(1) in functions:
            out.append((line_at(newlines, m.start(1)), m.group(1), SITE_HANDLER))
    seen: set[tuple[int, str, str]] = set()
    uniq = [s for s in out if not (s in seen or seen.add(s))]  # type: ignore[func-returns-value]
    uniq.sort(key=lambda t: t[0])
    return uniq


__all__ = [
    "astro_blocks",
    "barrel_alias_map",
    "component_unit_of",
    "default_import_bindings",
    "import_target_edges",
    "import_targets",
    "is_astro_path",
    "is_sfc_path",
    "is_svelte_path",
    "markup_range",
    "markup_sites",
    "parse_astro",
    "parse_svelte",
    "sfc_component_name",
]
