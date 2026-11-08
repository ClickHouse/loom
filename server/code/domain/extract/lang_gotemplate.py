"""Go templates and Helm charts: defines, includes, manifests.

A Helm chart's `templates/` directory is Go `text/template` over YAML: the
`_helpers.tpl` files hold `{{ define "chart.fullname" }} … {{ end }}`
blocks and every manifest calls them (`{{ include "chart.labels" . |
nindent 4 }}`, `{{ template "chart.name" . }}`). This lane reads the
actions with a scanner (no tree-sitter grammar):

  * `define` / `block` → a `function` symbol named as the define is
    (`chart.fullname`), qualified `<mod>::chart.fullname`, spanning to the
    matching `end` (`if` / `range` / `with` / `block` / `define` open a
    level, `end` closes one, `else` changes none);
  * a manifest's YAML documents (`---`) with a column-0 `kind:` → a `type`
    symbol per document named by the Kind (`Deployment`), signature the
    `metadata.name` expression, spanning the document — the unit a
    template's includes bind from and `code.outline` lists;
  * `include "x"` / `template "x"` anywhere in an action, pipelines
    included → `include` sites, bound to the define by `lane_units.
    bind_sites` (same chart at 1.0 through the same-file rung when the
    define is in the file; otherwise the repository-unique define at 0.6 —
    a define name shared by several charts stays unbound, deliberately);
  * `Chart.yaml` `dependencies[]` → include edges to the dependency charts
    the repository holds (`chart_dependency_candidates`).

Detection: `.tpl` / `.tmpl` / `.gotmpl` by extension (`lang_detect`), and
`is_template_manifest` for YAML / text / JSON / HTML under a `templates/`
directory whose text holds an action. Outside the extractor stamp.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lane_units import SiteRule
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_GOTEMPLATE = "gotemplate"

KIND_DEFINE = "function"
KIND_DOCUMENT = "type"
SITE_INCLUDE = "include"

SITE_RULES: dict[str, SiteRule] = {
    SITE_INCLUDE: SiteRule("calls", frozenset({KIND_DEFINE})),
}

# how `lane_units.span_chunks` cuts a template: `{{/* … */}}` comments ride
# the define below them
CHUNK_OPTIONS: dict[str, Any] = {
    "comment_leaders": ("{{/*", "{{- /*", "{{-/*", "#"),
    "comment_blocks": (("/*", "*/}}"), ("/*", "*/ -}}"), ("/*", "*/-}}")),
    "header_prefixes": (),
}
_MANIFEST_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml", ".txt", ".json", ".html"})

# A body stops at the next `{{`, so an unclosed action is not searched on from each; the
# trim markers and spaces are stripped in `_actions`, not by `\s*` around a lazy body.
_ACTION_RE = re.compile(r"\{\{-?((?:(?!\{\{).)*?)\}\}", re.S)
_COMMENT_RE = re.compile(r"\{\{-?\s*/\*(?:(?!\{\{).)*?\*/\s*-?\}\}", re.S)
_NAMED_RE = re.compile(r'^(define|block)\s+"([^"]+)"')
_INCLUDE_RE = re.compile(r'(?<![\w.$])(?:include|template)\s+"([^"]+)"')
_OPENERS: frozenset[str] = frozenset({"if", "range", "with", "define", "block"})
_KIND_RE = re.compile(r"^kind:\s*([A-Za-z][\w.-]*)\s*$", re.M)
_NAME_RE = re.compile(r"^  name:\s*(.+?)\s*$")


@dataclass
class TemplateUnits:
    symbols: list[Symbol] = field(default_factory=list)
    sites: list[tuple[int, str, str]] = field(default_factory=list)


def _ext(path: str) -> str:
    name = path.lower().rsplit("/", 1)[-1]
    return name[name.rfind(".") :] if "." in name else ""


def is_template_manifest(path: str, source: bytes) -> bool:
    """A YAML / text / JSON / HTML file under a `templates/` directory whose
    text carries a template action — a Helm manifest template, a Go
    `html/template` page — indexes as a template, not as config."""
    if _ext(path) not in _MANIFEST_EXTENSIONS:
        return False
    if not (path.startswith("templates/") or "/templates/" in path):
        return False
    head = source[:200_000]
    return b"{{" in head and b"}}" in head


def _actions(text: str) -> list[tuple[int, int, str]]:
    """(start line, end line, inner text) of every action, comments dropped."""
    out: list[tuple[int, int, str]] = []
    # blank the comments but keep their newlines so line numbers hold
    cleaned = _COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    line, at = 1, 0
    for m in _ACTION_RE.finditer(cleaned):
        line += cleaned.count("\n", at, m.start())  # from the last action, not the top
        at = m.start()
        end = line + m.group(0).count("\n")
        inner = m.group(1)
        inner = inner[:-1] if inner.endswith("-") else inner  # the `-}}` trim marker
        out.append((line, end, inner.strip()))
    return out


def _document_symbols(path: str, mod: str, text: str) -> list[Symbol]:
    """One `type` symbol per YAML document with a column-0 `kind:`."""
    lines = text.split("\n")
    n = len(lines)
    starts = [0] + [i + 1 for i, ln in enumerate(lines) if ln.rstrip() == "---"]
    out: list[Symbol] = []
    for idx, s in enumerate(starts):
        e = (starts[idx + 1] - 2) if idx + 1 < len(starts) else n - 1
        while e > s and not lines[e].strip():
            e -= 1
        if e < s:
            continue
        body = "\n".join(lines[s : e + 1])
        km = _KIND_RE.search(body)
        if km is None:
            continue
        name = ""
        in_meta = False
        for ln in lines[s : e + 1]:
            if ln.startswith("metadata:"):
                in_meta = True
                continue
            if in_meta:
                if ln[:1] not in (" ", "\t"):
                    in_meta = False
                    continue
                nm = _NAME_RE.match(ln)
                if nm:
                    name = nm.group(1)
                    break
        kind_name = km.group(1)
        out.append(
            Symbol(
                qualified_name=f"{mod}::{kind_name}",
                name=kind_name,
                kind=KIND_DOCUMENT,
                signature=f"{kind_name} {name}".strip(),
                path=path,
                start_line=s + 1,
                end_line=e + 1,
            )
        )
    return out


def template_units(path: str, text: str) -> TemplateUnits:
    """Defines (with their spans), document symbols and include sites."""
    mod = module_scope(path)
    units = TemplateUnits()
    stack: list[tuple[str, str, int]] = []  # (opener, define name or '', start line)
    for start, end, inner in _actions(text):
        for m in _INCLUDE_RE.finditer(inner):
            units.sites.append((start, m.group(1), SITE_INCLUDE))
        word = inner.split(None, 1)[0] if inner else ""
        named = _NAMED_RE.match(inner)
        if named:
            stack.append((named.group(1), named.group(2), start))
        elif word in _OPENERS:
            stack.append((word, "", start))
        elif word == "end":
            if stack:
                opener, name, s_line = stack.pop()
                if name:
                    units.symbols.append(
                        Symbol(
                            qualified_name=f"{mod}::{name}",
                            name=name,
                            kind=KIND_DEFINE,
                            signature=f'{opener} "{name}"',
                            path=path,
                            start_line=s_line,
                            end_line=end,
                        )
                    )
    # an unterminated define runs to the end of the file
    last_line = text.count("\n") + 1
    for opener, name, s_line in stack:
        if name:
            units.symbols.append(
                Symbol(
                    qualified_name=f"{mod}::{name}",
                    name=name,
                    kind=KIND_DEFINE,
                    signature=f'{opener} "{name}"',
                    path=path,
                    start_line=s_line,
                    end_line=last_line,
                )
            )
    if _ext(path) in _MANIFEST_EXTENSIONS or not units.symbols:
        units.symbols.extend(_document_symbols(path, mod, text))
    units.symbols.sort(key=lambda s: (s.start_line, -s.end_line))
    return units


def parse_gotemplate(path: str, source: bytes) -> ParsedFile:
    pf = ParsedFile(path=path, lang=LANG_GOTEMPLATE, source=source)
    pf.symbols = template_units(path, source.decode("utf-8", "replace")).symbols
    return pf


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    return template_units(pf.path, pf.source.decode("utf-8", "replace")).sites


def wanted_paths(from_path: str, specs: list[str]) -> list[str]:
    """A template names no repository file by specifier (`include` names a
    define, bound as a site); kept for the lane contract."""
    return []


def resolve_imports(from_path: str, specs: list[str], present: set[str]) -> list[str]:
    return []


# ------------------------------------------------------------- Chart.yaml


def is_chart_manifest(path: str) -> bool:
    return posixpath.basename(path) == "Chart.yaml"


def chart_dependency_candidates(path: str, text: str) -> list[str]:
    """Repository paths the chart's `dependencies[]` may name — a sibling
    chart directory (`<charts root>/<name>/Chart.yaml`), a vendored
    `charts/<name>/Chart.yaml`, `helm-charts/<name>/Chart.yaml`, and the
    directory a `file://` repository points at — for one membership read.
    An OCI or HTTPS repository names nothing in the tree."""
    try:
        import yaml  # stubs: types-PyYAML (dev extra)

        doc: Any = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — a malformed chart manifest names nothing
        return []
    deps = doc.get("dependencies") if isinstance(doc, dict) else None
    if not isinstance(deps, list):
        return []
    chart_dir = posixpath.dirname(path)
    parent = posixpath.dirname(chart_dir)
    out: list[str] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "").strip()
        repo = str(dep.get("repository") or "").strip()
        cands: list[str] = []
        if repo.startswith("file://"):
            rel = repo[len("file://") :]
            target = (
                rel.lstrip("/")
                if rel.startswith("/")
                else posixpath.normpath(posixpath.join(chart_dir, rel))
            )
            if not target.startswith(".."):
                cands.append(f"{target}/Chart.yaml")
        if name:
            cands.append(f"{chart_dir}/charts/{name}/Chart.yaml")
            if parent:
                cands.append(f"{parent}/{name}/Chart.yaml")
            cands.append(f"helm-charts/{name}/Chart.yaml")
        out.extend(c for c in cands if c not in out and c != path)
    return out


__all__ = [
    "CHUNK_OPTIONS",
    "KIND_DEFINE",
    "KIND_DOCUMENT",
    "LANG_GOTEMPLATE",
    "SITE_INCLUDE",
    "SITE_RULES",
    "TemplateUnits",
    "chart_dependency_candidates",
    "is_chart_manifest",
    "is_template_manifest",
    "parse_gotemplate",
    "reference_sites",
    "resolve_imports",
    "template_units",
    "wanted_paths",
]
