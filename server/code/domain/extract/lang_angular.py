"""Angular components and their templates.

An Angular component is a class under `@Component({ selector: 'app-x',
templateUrl: './x.component.html' | template: `…` })`. Its template
renders other components BY SELECTOR (`<app-header />`), never by an
import the template file could name, and wires the class's methods through
event bindings (`(click)="save()"`), interpolations (`{{ fmt(row) }}`),
structural directives (`*ngIf="check(x)"`) and property bindings
(`[items]="rows()"`).

The lane gives a reader what the other component lanes give:
- the component class carries `selector:<name>` in its signature, so a
  selector is a namespace-wide fact one keyed read answers
  (`CodeRepo.symbols_by_signatures`);
- an external template `x.component.html` is a `function` unit
  `<Class>::template` spanning the file, owned by the sibling
  `x.component.ts` (the CLI convention; `templateUrl` when it says
  otherwise), so the template's references attribute to the component;
- a kebab-case element whose selector a component declares is a `calls`
  edge from the template unit (or the class, for an inline template) to
  that component class at 1.0; an event, interpolation, directive or
  property call is a `calls` edge to the component's own method at 1.0.

Standard elements (`div`, `router-outlet`'s library selectors) match no
declared selector and yield nothing. Like the other lane parsers, outside
the extractor stamp: a namespace gains the rows by a targeted backfill.
"""

from __future__ import annotations

import bisect
import dataclasses
import posixpath
import re

from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol

SELECTOR_SIG = "selector:"
TEMPLATE_UNIT = "template"
_COMPONENT_RE = re.compile(
    r"@Component\s*\(\s*\{(?P<meta>(?:(?!@Component).)*?)\}\s*\)\s*(?:export\s+)?(?:default\s+)?"
    r"class\s+(?P<cls>[A-Za-z_$][\w$]*)",
    re.S,
)
_SELECTOR_RE = re.compile(r"""\bselector\s*:\s*['"`]([^'"`]+)['"`]""")
_TEMPLATE_URL_RE = re.compile(r"""\btemplateUrl\s*:\s*['"`]([^'"`]+)['"`]""")
_INLINE_TEMPLATE_RE = re.compile(r"\btemplate\s*:\s*`")
_KEBAB_TAG_RE = re.compile(r"<([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?=[\s/>])")
# `(click)="save()"`, `(ngSubmit)="submit($event)"`, `(click)="save"`
_EVENT_RE = re.compile(r"""\(([\w.]+)\)\s*=\s*["']\s*([A-Za-z_$][\w$]*)\s*[(\s"']""")
# `{{ fmt(row) }}`, `{{ items().length }}`
# The body stops at the next `{{`: an unclosed one would search on from each.
_INTERP_RE = re.compile(r"\{\{(?:(?!\{\{)[^}])*?(?<![\w$])([A-Za-z_$][\w$]*+)\s*\(")
# `*ngIf="check(x)"`, `[items]="rows()"`, `@if (loaded())` (control flow)
_BOUND_CALL_RE = re.compile(
    r"""(?:\*[A-Za-z_]\w*|\[[\w.]+\]|@(?:if|for|switch|else if))\s*[=(]\s*+["'(]?"""
    r"""(?:(?!\*[A-Za-z_]\w*\s*=|\[[\w.]+\]\s*[=(]|@(?:if|for|switch|else if)\b)[^"')])*?"""
    r"""(?<![\w$])([A-Za-z_$][\w$]*+)\s*\("""
)


def is_angular_template(path: str) -> bool:
    return path.lower().endswith(".component.html")


def component_meta(source: str) -> list[dict[str, str]]:
    """[{class, selector, template_url, inline}] for every `@Component`
    class in a TypeScript source; `inline` is the template literal's text
    ('' when the template is external)."""
    out: list[dict[str, str]] = []
    # counted on from the last class: a count from the top per class is quadratic
    counted, line = 0, 1
    for m in _COMPONENT_RE.finditer(source):
        line += source.count("\n", counted, m.start())
        counted = m.start()
        meta = m.group("meta")
        sel = _SELECTOR_RE.search(meta)
        url = _TEMPLATE_URL_RE.search(meta)
        inline = ""
        im = _INLINE_TEMPLATE_RE.search(meta)
        if im is not None:
            end = meta.find("`", im.end())
            inline = meta[im.end() : end if end >= 0 else len(meta)]
        out.append(
            {
                "class": m.group("cls"),
                "selector": sel.group(1).strip() if sel else "",
                "template_url": url.group(1).strip() if url else "",
                "inline": inline,
                "meta_start": str(line),
            }
        )
    return out


def stamp_selectors(symbols: list[Symbol], source: bytes) -> list[Symbol]:
    """The symbols with each `@Component` class carrying the signature
    `selector:<name>` (part of the symbol id: a selector change is a new row)."""
    metas = {m["class"]: m for m in component_meta(source.decode("utf-8", "replace"))}
    if not metas:
        return symbols
    return [
        dataclasses.replace(s, signature=f"{SELECTOR_SIG}{metas[s.name]['selector']}")
        if s.kind == "class" and s.name in metas and metas[s.name]["selector"]
        else s
        for s in symbols
    ]


def owner_path(template_path: str) -> str:
    """The component file the CLI convention pairs with a template:
    `x.component.html` → `x.component.ts`."""
    return template_path[: -len(".html")] + ".ts"


def owner_class_from(template_path: str, owner_ts: str, owner_source: str) -> str | None:
    """`owner_class` for a component file that is not the CLI sibling: the
    class whose `templateUrl` names the template, in `owner_ts`."""
    base = posixpath.basename(template_path)
    pick = next(
        (m for m in component_meta(owner_source) if posixpath.basename(m["template_url"]) == base),
        None,
    )
    return f"{module_scope(owner_ts)}::{pick['class']}" if pick else None


def owner_class(template_path: str, owner_source: str) -> str | None:
    """The qualified name of the component class an external template
    belongs to: the owner's class whose `templateUrl` names this file, else
    its first component class; None when the owner declares none."""
    metas = component_meta(owner_source)
    base = posixpath.basename(template_path)
    pick = next((m for m in metas if posixpath.basename(m["template_url"]) == base), None) or (
        metas[0] if metas else None
    )
    return f"{module_scope(owner_path(template_path))}::{pick['class']}" if pick else None


def template_sites(text: str, base_line: int = 1) -> list[tuple[int, str, str]]:
    """(line, name, kind): `selector` sites for kebab-case elements,
    `handler` sites for events, interpolation calls, directive and
    property-binding calls; source order, deduplicated."""
    out: list[tuple[int, str, str]] = []

    # by bisection in the newlines found once: a count from the top per site is quadratic
    newlines = [m.start() for m in re.finditer("\n", text)]

    def line_of(offset: int) -> int:
        return base_line + bisect.bisect_left(newlines, offset)

    for m in _KEBAB_TAG_RE.finditer(text):
        out.append((line_of(m.start()), m.group(1), "selector"))
    for m in _EVENT_RE.finditer(text):
        out.append((line_of(m.start(2)), m.group(2), "handler"))
    for rx in (_INTERP_RE, _BOUND_CALL_RE):
        for m in rx.finditer(text):
            out.append((line_of(m.start(1)), m.group(1), "handler"))
    seen: set[tuple[int, str, str]] = set()
    uniq = [s for s in out if not (s in seen or seen.add(s))]  # type: ignore[func-returns-value]
    uniq.sort(key=lambda t: t[0])
    return uniq


def parse_angular_template(path: str, source: bytes) -> ParsedFile:
    """An external template: one `function` unit `<module>::template`
    spanning the file (the owning class is read at edge time, when the
    repository is at hand); no tree, `typescript` tag so the reference pass
    runs and every downstream branch applies."""
    text = source.decode("utf-8", "replace")
    pf = ParsedFile(path=path, lang="typescript", source=source)
    end = max(1, text.count("\n") + (0 if text.endswith("\n") else 1))
    pf.symbols.append(
        Symbol(
            qualified_name=f"{module_scope(path)}::{TEMPLATE_UNIT}",
            name=TEMPLATE_UNIT,
            kind="function",
            signature="template",
            path=path,
            start_line=1,
            end_line=end,
            has_body=True,
        )
    )
    return pf


def inline_template_sites(pf: ParsedFile) -> list[tuple[int, str, str, str]]:
    """(line, name, kind, class qualified name) for the sites of every inline
    template in a component file."""
    text = pf.source.decode("utf-8", "replace")
    out: list[tuple[int, str, str, str]] = []
    newlines = [m.start() for m in re.finditer("\n", text)]
    for m in component_meta(text):
        if not m["inline"]:
            continue
        start = text.find(m["inline"])
        base = bisect.bisect_left(newlines, start) + 1 if start >= 0 else int(m["meta_start"])
        cls = f"{module_scope(pf.path)}::{m['class']}"
        for line, name, kind in template_sites(m["inline"], base):
            out.append((line, name, kind, cls))
    return out


def template_edges(
    pf: ParsedFile,
    sites: list[tuple[int, str, str]],
    src: Symbol,
    selectors: dict[str, tuple[str, str]],
    methods: dict[str, list[tuple[str, str]]],
) -> list[Edge]:
    """`calls` edges from `src` (the template unit, or the class for an inline
    template): a `selector` site to the component `selectors[name]` = (id,
    qualified) declares; a `handler` site to the one method of the owning
    class named so (`methods[name]` = [(id, qualified)])."""
    edges: list[Edge] = []
    seen: set[tuple[str, int]] = set()
    for line, name, kind in sites:
        target = None
        if kind == "selector":
            target = selectors.get(name)
        elif kind == "handler" and len(methods.get(name, [])) == 1:
            target = methods[name][0]
        if target is None or target[1] == src.qualified_name or (target[0], line) in seen:
            continue
        seen.add((target[0], line))
        edges.append(
            Edge("calls", src.id, target[0], src.qualified_name, target[1], pf.path, line, 1.0)
        )
    return edges


__all__ = [
    "SELECTOR_SIG",
    "TEMPLATE_UNIT",
    "component_meta",
    "inline_template_sites",
    "is_angular_template",
    "owner_class",
    "owner_class_from",
    "owner_path",
    "parse_angular_template",
    "stamp_selectors",
    "template_edges",
    "template_sites",
]
