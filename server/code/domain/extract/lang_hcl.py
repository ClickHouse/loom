"""Terraform and terragrunt: blocks are symbols, references are edges.

A platform repository declares its infrastructure in HCL beside the Go that runs on it,
and a text index sees none of it as more than text. HCL's block grammar is small and
regular — `<type> "label" ["label"] { … }` with `name = expr` attributes — so a token
scanner with brace tracking reads it, like the proto lane; no tree-sitter grammar is
added to the image.

Symbols, each named the way Terraform's own expressions spell it, so a
reference binds by exact name and `code.symbol var.region` finds it:

  * `resource "aws_s3_bucket" "b"`   → `aws_s3_bucket.b`   kind `resource`
  * `data "aws_ami" "x"`             → `data.aws_ami.x`    kind `data`
  * `module "vpc"`                   → `module.vpc`        kind `module`
  * `variable "region"`              → `var.region`        kind `var`
  * `output "id"`                    → `output.id`         kind `output`
  * `locals { a = … }`               → `local.a`           kind `const`
  * `provider "aws"`                 → `provider.aws`      kind `provider`
  * terragrunt `dependency "vpc"`    → `dependency.vpc`    kind `dependency`
  * terragrunt `include "root"`      → `include.root`      kind `include`
  * terragrunt `generate "x"`        → `generate.x`        kind `generate`

The qualified name is `<dotted directory scope>::<name>`: Terraform's unit is
the directory (a module), so two modules' `var.region` stay apart and one
module's files share a scope. Reference sites (`var.x`, `local.x`,
`module.x.attr`, `data.t.n`, `aws_type.name`, `dependency.x.outputs.y`)
bind through `lane_units.bind_sites`: the same module's definition first,
else the repository-unique one, as `reads` edges. Module `source` paths and
terragrunt `config_path` / `source` that name a repository directory become
`includes` edges to that directory's conventional files, settled against the
namespace's live paths; a registry or git source names nothing in the tree.
`find_in_parent_folders()` names the nearest ancestor `terragrunt.hcl`
(or `root.hcl`). `.tfvars` files are chunk-only (values, not declarations).
Outside the extractor stamp with the other lanes.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lane_units import SiteRule
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_HCL = "hcl"

KIND_RESOURCE = "resource"
KIND_DATA = "data"
KIND_MODULE = "module"
KIND_VAR = "var"
KIND_OUTPUT = "output"
KIND_LOCAL = "const"
KIND_PROVIDER = "provider"
KIND_DEPENDENCY = "dependency"
KIND_INCLUDE = "include"
KIND_GENERATE = "generate"

SITE_VAR = "var_ref"
SITE_LOCAL = "local_ref"
SITE_MODULE = "module_ref"
SITE_DATA = "data_ref"
SITE_RESOURCE = "resource_ref"
SITE_DEPENDENCY = "dependency_ref"
SITE_PROVIDER = "provider_ref"

SITE_RULES: dict[str, SiteRule] = {
    SITE_VAR: SiteRule("reads", frozenset({KIND_VAR})),
    SITE_LOCAL: SiteRule("reads", frozenset({KIND_LOCAL})),
    SITE_MODULE: SiteRule("reads", frozenset({KIND_MODULE})),
    SITE_DATA: SiteRule("reads", frozenset({KIND_DATA})),
    SITE_RESOURCE: SiteRule("reads", frozenset({KIND_RESOURCE})),
    SITE_DEPENDENCY: SiteRule("reads", frozenset({KIND_DEPENDENCY})),
    SITE_PROVIDER: SiteRule("reads", frozenset({KIND_PROVIDER})),
}

CHUNK_OPTIONS: dict[str, Any] = {
    "comment_leaders": ("#", "//", "/*", "*"),
    "comment_blocks": (("/*", "*/"),),
    "header_prefixes": ("terraform", "provider", "required_providers"),
}

MODULE_FILES: tuple[str, ...] = (
    "main.tf",
    "variables.tf",
    "outputs.tf",
    "versions.tf",
    "providers.tf",
)
TERRAGRUNT_FILES: tuple[str, ...] = ("terragrunt.hcl", "root.hcl")

_TOKEN_RE = re.compile(
    # An unclosed comment or heredoc runs to the end instead of being searched again from
    # every later opener; a string's parts are matched possessively, an interpolation
    # first, so a string that never closes is not retried every way it could split; an
    # unclosed one is one token to the end of its line, or each `\"` in it would start
    # another scan of the rest.
    r"#[^\n]*|//[^\n]*|/\*.*?(?:\*/|\Z)"
    r"|<<-?(?P<heredoc>[A-Z_][A-Z0-9_]*)\n.*?(?:\n[^\S\n]*(?P=heredoc)|\Z)"
    r"|\"(?:(?>\$\{(?:[^{}]|\{[^{}]*\})*\})|[^\"\\\n]|\\.)*+\"?"
    r"|[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*|-?\d[\w.]*|[{}()\[\]=,:?]|\S",
    re.S,
)
_REF_RE = re.compile(
    r"(?<![\w.\"-])(?:"
    r"(?P<var>var\.[A-Za-z_][\w-]*)"
    r"|(?P<local>local\.[A-Za-z_][\w-]*)"
    r"|(?P<module>module\.[A-Za-z_][\w-]*)"
    r"|(?P<data>data\.[a-z][a-z0-9]*_[a-z0-9_]+\.[A-Za-z_][\w-]*)"
    r"|(?P<dependency>dependency\.[A-Za-z_][\w-]*)"
    r"|(?P<resource>[a-z][a-z0-9]*_[a-z0-9_]+\.[A-Za-z_][\w-]*)"
    r")(?![\w-]*\s*[={])"
)
_PARENT_FN = "find_in_parent_folders()"
_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "resource",
        "data",
        "module",
        "variable",
        "output",
        "locals",
        "provider",
        "terraform",
        "dependency",
        "dependencies",
        "include",
        "generate",
        "remote_state",
        "moved",
        "import",
        "check",
        "removed",
    }
)


@dataclass
class _Tok:
    text: str
    line: int


@dataclass
class HclUnits:
    symbols: list[Symbol] = field(default_factory=list)
    sites: list[tuple[int, str, str]] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


def hcl_scope(path: str) -> str:
    """The module's scope: its directory dotted, `root` for the top level —
    every file of one directory shares it (Terraform's unit is the module)."""
    d = posixpath.dirname(path).strip("/")
    return d.replace("/", ".") if d else "root"


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    line = 1
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        t = m.group(0)
        if t.startswith(("#", "//", "/*")):
            continue
        out.append(_Tok(t, line))
    return out


class _Parser:
    def __init__(self, path: str, toks: list[_Tok]) -> None:
        self.path, self.toks, self.i = path, toks, 0
        self.scope = hcl_scope(path)
        self.units = HclUnits()

    def _peek(self, k: int = 0) -> _Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def _next(self) -> _Tok | None:
        t = self._peek()
        if t is not None:
            self.i += 1
        return t

    def _block_end(self) -> int:
        """At `{`: consume the balanced block, return its closing line."""
        depth = 0
        last = 0
        while (t := self._next()) is not None:
            last = t.line
            if t.text in ("{", "[", "("):
                depth += 1
            elif t.text in ("}", "]", ")"):
                depth -= 1
                if depth <= 0:
                    return last
        return last

    def _add(self, name: str, kind: str, sig: str, start: int, end: int) -> None:
        self.units.symbols.append(
            Symbol(
                qualified_name=f"{self.scope}::{name}",
                name=name,
                kind=kind,
                signature=sig,
                path=self.path,
                start_line=start,
                end_line=end,
            )
        )

    def _labels(self) -> list[str]:
        out: list[str] = []
        while (t := self._peek()) is not None and t.text.startswith('"'):
            self._next()
            out.append(t.text.strip('"'))
        return out

    def _attr_value(self, name: str, start_i: int, end_i: int) -> str:
        """`name = "value"` at the block's own level (not inside a nested
        block or object), the string's text."""
        depth = 0
        for j in range(start_i, min(end_i, len(self.toks)) - 2):
            t = self.toks[j]
            if t.text in ("{", "[", "("):
                depth += 1
                continue
            if t.text in ("}", "]", ")"):
                depth -= 1
                continue
            quoted = self.toks[j + 2].text.startswith('"')
            if depth == 0 and t.text == name and self.toks[j + 1].text == "=" and quoted:
                return self.toks[j + 2].text.strip('"')
        return ""

    def parse(self) -> HclUnits:
        while (t := self._peek()) is not None:
            if (
                t.text in _BLOCK_TYPES
                and self._peek(1) is not None
                and (
                    self._peek(1).text.startswith('"') or self._peek(1).text == "{"  # type: ignore[union-attr]
                )
            ):
                self._block()
                continue
            # a top-level attribute (`inputs = {…}` in terragrunt, a tfvars value)
            self._next()
            if (n := self._peek()) is not None and n.text == "=":
                self._next()
                v = self._peek()
                if v is not None and v.text in ("{", "["):
                    self._block_end()
                else:
                    self._next()
        return self.units

    def _block(self) -> None:
        head = self._next()
        assert head is not None
        labels = self._labels()
        opener = self._peek()
        if opener is None or opener.text != "{":
            return
        body_start = self.i + 1
        end = self._block_end()
        body_end = self.i
        kind_word = head.text
        if kind_word == "locals":
            self._locals(body_start, body_end, head.line)
            return
        if kind_word == "terraform":
            src = self._attr_value("source", body_start, body_end)
            if src and (_is_local_source(src) or "//" in src):
                self.units.imports.append(src)  # terragrunt's module source
            return
        if kind_word in ("dependencies", "remote_state", "moved", "import", "check", "removed"):
            return
        name, kind, sig = self._named_block(kind_word, labels, body_start, body_end)
        if name:
            if kind in (KIND_RESOURCE, KIND_DATA, KIND_MODULE):
                sig += self._repetition(body_start, body_end)
            self._add(name, kind, sig, head.line, end)

    def _named_block(
        self,
        kind_word: str,
        labels: list[str],
        body_start: int,
        body_end: int,
    ) -> tuple[str, str, str]:
        """(name, kind, signature) of a declaring block; ('', '', '') for one
        the lane does not name."""
        name, kind, sig = "", "", ""
        if kind_word == "resource" and len(labels) >= 2:
            name, kind, sig = f"{labels[0]}.{labels[1]}", KIND_RESOURCE, f'resource "{labels[0]}"'
        elif kind_word == "data" and len(labels) >= 2:
            name, kind, sig = f"data.{labels[0]}.{labels[1]}", KIND_DATA, f'data "{labels[0]}"'
        elif kind_word == "module" and labels:
            src = self._attr_value("source", body_start, body_end)
            name, kind, sig = f"module.{labels[0]}", KIND_MODULE, f'module source="{src}"'
            if src:
                self.units.imports.append(src)
        elif kind_word == "variable" and labels:
            typ = self._attr_value("type", body_start, body_end) or self._raw_attr(
                "type", body_start, body_end
            )
            name, kind, sig = f"var.{labels[0]}", KIND_VAR, f"variable {typ}".rstrip()
        elif kind_word == "output" and labels:
            name, kind, sig = f"output.{labels[0]}", KIND_OUTPUT, "output"
        elif kind_word == "provider" and labels:
            # An aliased provider is its own symbol
            alias = self._attr_value("alias", body_start, body_end)
            name = f"provider.{labels[0]}" + (f".{alias}" if alias else "")
            kind = KIND_PROVIDER
            sig = f'provider "{labels[0]}"' + (f' alias "{alias}"' if alias else "")
        elif kind_word == "dependency" and labels:
            cfg = self._attr_value("config_path", body_start, body_end)
            name, kind, sig = f"dependency.{labels[0]}", KIND_DEPENDENCY, f'dependency "{cfg}"'
            if cfg:
                self.units.imports.append(cfg)
        elif kind_word == "include":
            label = labels[0] if labels else "root"
            path_val = self._attr_value("path", body_start, body_end) or self._raw_attr(
                "path", body_start, body_end
            )
            name, kind, sig = f"include.{label}", KIND_INCLUDE, f"include {path_val}".rstrip()
            if path_val:
                self.units.imports.append(path_val)
        elif kind_word == "generate" and labels:
            name, kind, sig = f"generate.{labels[0]}", KIND_GENERATE, "generate"
        return name, kind, sig

    def _repetition(self, start_i: int, end_i: int) -> str:
        """` [count]` / ` [for_each]` when the block's own level repeats it — the agent
        sees that `aws_subnet.a` is N instances."""
        if self._raw_attr("count", start_i, end_i):
            return " [count]"
        if self._raw_attr("for_each", start_i, end_i):
            return " [for_each]"
        return ""

    def _raw_attr(self, name: str, start_i: int, end_i: int) -> str:
        """`name = expr` at the block's own level, the expression's first
        token — a call keeps its parentheses (`find_in_parent_folders()`)."""
        depth = 0
        for j in range(start_i, min(end_i, len(self.toks)) - 2):
            t = self.toks[j]
            if t.text in ("{", "[", "("):
                depth += 1
                continue
            if t.text in ("}", "]", ")"):
                depth -= 1
                continue
            if depth == 0 and t.text == name and self.toks[j + 1].text == "=":
                v = self.toks[j + 2].text
                if j + 3 < len(self.toks) and self.toks[j + 3].text == "(":
                    # a call keeps its arguments: `map(string)`, `find_in_parent_folders()`
                    inner: list[str] = []
                    d = 0
                    for u in self.toks[j + 3 : min(end_i, len(self.toks))]:
                        if u.text == "(":
                            d += 1
                            if d == 1:
                                continue
                        elif u.text == ")":
                            d -= 1
                            if d == 0:
                                break
                        inner.append(u.text)
                    return f"{v}({''.join(inner)})"
                return v
        return ""

    def _locals(self, start_i: int, end_i: int, head_line: int) -> None:
        """Each top-level `a = …` of a `locals` block is `local.a`."""
        j = start_i
        depth = 0
        while j < end_i - 1:
            t = self.toks[j]
            if t.text in ("{", "[", "("):
                depth += 1
            elif t.text in ("}", "]", ")"):
                depth -= 1
            elif (
                depth == 0
                and self.toks[j + 1].text == "="
                and re.fullmatch(r"[A-Za-z_][\w-]*", t.text)
            ):
                # the value's extent: to the next depth-0 attribute or the block's end
                k = j + 2
                d = 0
                while k < end_i:
                    u = self.toks[k]
                    if u.text in ("{", "[", "("):
                        d += 1
                    elif u.text in ("}", "]", ")"):
                        d -= 1
                    elif d == 0 and k + 1 < end_i and self.toks[k + 1].text == "=" and k > j + 2:
                        break
                    k += 1
                end_line = self.toks[min(k, end_i) - 1].line if k > j + 2 else t.line
                self._add(f"local.{t.text}", KIND_LOCAL, "local", t.line, max(end_line, t.line))
            j += 1
        _ = head_line


def hcl_units(path: str, text: str) -> HclUnits:
    units = _Parser(path, _tokens(text)).parse()
    if not path.endswith(".tfvars"):
        units.sites = _reference_sites(text)
    return units


_PROVIDER_ATTR_RE = re.compile(r"^\s*provider\s*=\s*([a-z][\w-]*)\.([A-Za-z_][\w-]*)\s*$")
# A key starts after no word or `-`: `\b` would start one at every letter of `a-a-a…`.
_PROVIDERS_MAP_RE = re.compile(r"(?<![\w-])[a-z][\w-]*+\s*=\s*([a-z][\w-]*)\.([A-Za-z_][\w-]*)")


def _provider_sites(
    code: str,
    ln: int,
    in_providers: bool,
) -> tuple[list[tuple[int, str, str]], bool]:
    """`provider = aws.west` names `provider.aws.west`; so do the values of a module's
    `providers = { aws = aws.west }` map. Returns the sites and whether a `providers =
    {` map is still open."""
    out: list[tuple[int, str, str]] = []
    m = _PROVIDER_ATTR_RE.match(code)
    if m:
        return [(ln, f"provider.{m.group(1)}.{m.group(2)}", SITE_PROVIDER)], in_providers
    stripped = code.strip()
    if re.match(r"^providers\s*=\s*\{", stripped):
        in_providers = "}" not in stripped
        body = stripped.split("{", 1)[1]
    elif in_providers:
        body = stripped
        if stripped.startswith("}"):
            return out, False
    else:
        return out, in_providers
    for pm in _PROVIDERS_MAP_RE.finditer(body):
        out.append((ln, f"provider.{pm.group(1)}.{pm.group(2)}", SITE_PROVIDER))
    if "}" in body:
        in_providers = False
    return out, in_providers


def _reference_sites(text: str) -> list[tuple[int, str, str]]:
    """(line, name, site kind) for every expression reference, in order;
    a reference inside a comment is skipped."""
    out: list[tuple[int, str, str]] = []
    in_providers = False
    for ln, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0]
        if "//" in code and not code.strip().startswith('"'):
            code = code.split("//", 1)[0]
        provider_hits, in_providers = _provider_sites(code, ln, in_providers)
        if provider_hits:
            out.extend(provider_hits)
            continue
        for m in _REF_RE.finditer(code):
            kind = m.lastgroup or ""
            name = m.group(kind)
            site = {
                "var": SITE_VAR,
                "local": SITE_LOCAL,
                "module": SITE_MODULE,
                "data": SITE_DATA,
                "dependency": SITE_DEPENDENCY,
                "resource": SITE_RESOURCE,
            }[kind]
            out.append((ln, name, site))
    return out


def parse_hcl(path: str, source: bytes) -> ParsedFile:
    text = source.decode("utf-8", "replace")
    units = hcl_units(path, text)
    pf = ParsedFile(path=path, lang=LANG_HCL, source=source)
    pf.symbols = units.symbols
    pf.includes = list(dict.fromkeys(units.imports))
    return pf


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    return hcl_units(pf.path, pf.source.decode("utf-8", "replace")).sites


def _is_local_source(spec: str) -> bool:
    return spec.startswith(("./", "../")) or spec == "." or spec == ".."


def wanted_paths(from_path: str, specs: list[str]) -> list[str]:
    """Repository paths a module source, a dependency's config path or an
    include may name: the directory's conventional files (a module) or its
    terragrunt file; `find_in_parent_folders()` → every ancestor's
    terragrunt / root file. A registry, git or `//`-less remote source
    names nothing in the tree."""
    out: list[str] = []
    here = posixpath.dirname(from_path)
    for spec in specs:
        if spec.startswith(_PARENT_FN):
            d = here
            while True:
                parent = posixpath.dirname(d)
                if parent == d:
                    break
                d = parent
                out.extend(posixpath.join(d, f) if d else f for f in TERRAGRUNT_FILES)
            continue
        if not _is_local_source(spec):
            continue
        target = spec.split("//", 1)[0] if spec.count("//") == 1 else spec
        sub = spec.split("//", 1)[1] if spec.count("//") == 1 else ""
        d = posixpath.normpath(posixpath.join(here, target, sub)).strip("/")
        d = "" if d == "." else d
        for f in (*MODULE_FILES, *TERRAGRUNT_FILES):
            out.append(posixpath.join(d, f) if d else f)
    return list(dict.fromkeys(out))


def resolve_imports(from_path: str, specs: list[str], present: set[str]) -> list[str]:
    """The live paths among the candidates, nearest ancestor first for a
    `find_in_parent_folders()` include (one file), every present module file
    for a directory source."""
    out: list[str] = []
    here = posixpath.dirname(from_path)
    for spec in specs:
        if spec.startswith(_PARENT_FN):
            d = here
            found = ""
            while not found:
                parent = posixpath.dirname(d)
                if parent == d:
                    break
                d = parent
                for f in TERRAGRUNT_FILES:
                    cand = posixpath.join(d, f) if d else f
                    if cand in present and cand != from_path:
                        found = cand
                        break
            if found:
                out.append(found)
            continue
        for cand in wanted_paths(from_path, [spec]):
            if cand in present and cand != from_path:
                out.append(cand)
    return list(dict.fromkeys(out))


__all__ = [
    "CHUNK_OPTIONS",
    "LANG_HCL",
    "SITE_RULES",
    "HclUnits",
    "hcl_scope",
    "hcl_units",
    "parse_hcl",
    "reference_sites",
    "resolve_imports",
    "wanted_paths",
]
