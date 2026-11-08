"""Go directives, struct tags and framework markers.

What a Go file says outside its declarations is what an agent working in a
service repository greps for first, and a text index sees none of it as
more than text:

  * `//go:embed pattern…` above a `var` — the variable `reads` every
    repository file the patterns name (settled against the namespace's live
    paths under the package directory; Go's rules: a pattern is relative to
    the package directory, a directory names its whole tree, `*`/`?`/`[…]`
    match within one path segment, files whose name starts with `.` or `_`
    are skipped unless the pattern carries the `all:` prefix). The edge is
    stored, so a file added later under a pattern waits for the next
    re-derivation of the declaring file — the same rule as every lane edge;
  * `//go:generate <command>` — a `generate` facet named by the command's
    program (`protoc`, `go run`, `mockgen`), so "what generates this" is one
    keyed read instead of a grep over 150 files;
  * struct field tags — a `field_tag` facet per key:value (`json:user_id`,
    `yaml:name`, `db:name_col`, `env:PORT`, `protobuf:…` is skipped: the
    wire name is the field's own), so a wire name is looked up by facet;
  * `cobra.Command{Use: "serve [flags]"}` — a `command` facet named by the
    first word of `Use`, the CLI's surface in one read;
  * `// +kubebuilder:<kind>:…` and `// +k8s:…` markers — a `marker` facet
    named `kubebuilder:<kind>` / `k8s:<kind>`, and a `crd` facet named by
    the type under `+kubebuilder:object:root=true`.

Facets ride the symbol stage keyed (kind, name, line-as-ordinal): a Go file
may carry the same json key on two structs. Outside the extractor stamp with
the Go lane; a namespace gains the rows by a targeted backfill.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.treesitter import ParsedFile, Symbol, file_symbol_id, node_text

FACET_GENERATE = "generate"
FACET_FIELD_TAG = "field_tag"
FACET_COMMAND = "command"
FACET_MARKER = "marker"
FACET_CRD = "crd"
FACET_EMBED = "embed"
GO_FACET_KINDS: frozenset[str] = frozenset(
    {
        FACET_GENERATE,
        FACET_FIELD_TAG,
        FACET_COMMAND,
        FACET_MARKER,
        FACET_CRD,
        FACET_EMBED,
    }
)

_EMBED_RE = re.compile(r"^//go:embed\s+(.+?)\s*$")
_GENERATE_RE = re.compile(r"^//go:generate\s+(.+?)\s*$")
_MARKER_RE = re.compile(r"^//\s*\+(kubebuilder|k8s|optional|required|genclient)(?::([\w-]+))?")
_TAG_RE = re.compile(r'(?<!\w)(\w+):"([^"]*)"')  # from a word's start: linear in a long one
_SKIPPED_TAG_KEYS: frozenset[str] = frozenset({"protobuf", "protobuf_oneof"})
_MAX_FACETS_PER_FILE = 400


@dataclass
class EmbedDirective:
    var_qualified_name: str
    var_id: str
    patterns: list[str]
    line: int


@dataclass
class GoDirectives:
    embeds: list[EmbedDirective] = field(default_factory=list)
    facets: list[Facet] = field(default_factory=list)


def _split_patterns(rest: str) -> list[str]:
    """`templates/*.tmpl "a b" static` → the patterns, quotes removed."""
    out: list[str] = []
    for tok in re.findall(r'"[^"]*"|`[^`]*`|\S+', rest):
        out.append(tok.strip('"`'))
    return out


def _comments_above(node: Any, source: bytes) -> list[tuple[int, str]]:
    """The run of `comment` siblings immediately above `node`, top-down."""
    out: list[tuple[int, str]] = []
    prev = node.prev_sibling
    while prev is not None and prev.type == "comment":
        if prev.end_point[0] + 1 < (out[-1][0] if out else node.start_point[0] + 1) - 1:
            break
        out.append((prev.start_point[0] + 1, node_text(prev, source).rstrip()))
        prev = prev.prev_sibling
    out.reverse()
    return out


def _program_of(command: str) -> str:
    """`go run gen.go` → `go run`; `protoc --go_out=. x.proto` → `protoc`;
    `$GOPATH/bin/mockgen -source=…` → `mockgen`."""
    words = command.split()
    if not words:
        return ""
    head = posixpath.basename(words[0])
    if head == "go" and len(words) > 1:
        return f"go {words[1]}"
    return head[:100]


def _var_names(decl: Any, source: bytes) -> list[str]:
    names: list[str] = []
    for spec in decl.named_children:
        if spec.type != "var_spec":
            continue
        for c in spec.named_children:
            if c.type == "identifier":
                names.append(node_text(c, source))
    return names


def _struct_tags(spec: Any, source: bytes, type_name: str, out: list[Facet]) -> None:
    typ = spec.child_by_field_name("type")
    if typ is None or typ.type != "struct_type":
        return
    fields = next((c for c in typ.named_children if c.type == "field_declaration_list"), None)
    for fd in fields.named_children if fields is not None else []:
        if fd.type != "field_declaration":
            continue
        tag = fd.child_by_field_name("tag") or next(
            (c for c in fd.named_children if c.type == "raw_string_literal"), None
        )
        if tag is None:
            continue
        for key, value in _TAG_RE.findall(node_text(tag, source)):
            if key in _SKIPPED_TAG_KEYS:
                continue
            name = value.split(",", 1)[0].strip()
            if not name or name == "-":
                continue
            out.append(
                Facet(
                    FACET_FIELD_TAG,
                    f"{key}:{name}"[:200],
                    fd.start_point[0] + 1,
                    fd.start_point[0] + 1,
                    1.0,
                )
            )
    _ = type_name


def _cobra_commands(root: Any, source: bytes, out: list[Facet]) -> None:
    """`cobra.Command{Use: "serve [flags]", …}` → `command: serve`."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "composite_literal":
            t = n.child_by_field_name("type")
            if t is not None and node_text(t, source).endswith("cobra.Command"):
                body = n.child_by_field_name("body")
                for el in body.named_children if body is not None else []:
                    if el.type != "keyed_element" or not el.named_children:
                        continue
                    key = node_text(el.named_children[0], source)
                    if key != "Use" or len(el.named_children) < 2:
                        continue
                    raw = node_text(el.named_children[1], source).strip('"`')
                    word = raw.split()[0] if raw.split() else ""
                    if word:
                        out.append(
                            Facet(
                                FACET_COMMAND,
                                word[:200],
                                el.start_point[0] + 1,
                                el.start_point[0] + 1,
                                1.0,
                            )
                        )
        stack.extend(n.children)


def _markers(decl: Any, source: bytes, out: list[Facet]) -> None:
    """The `+kubebuilder:…` / `+k8s:…` markers above a declaration; a
    `+kubebuilder:object:root=true` names the CRD type."""
    for line, text in _comments_above(decl, source):
        m = _MARKER_RE.match(text)
        if not m:
            continue
        head = m.group(1) + (f":{m.group(2)}" if m.group(2) else "")
        out.append(Facet(FACET_MARKER, head, line, line, 1.0))
        if head == "kubebuilder:object" and "root=true" in text:
            for spec in decl.named_children:
                name = spec.child_by_field_name("name") if spec.type == "type_spec" else None
                if name is not None:
                    out.append(Facet(FACET_CRD, node_text(name, source), line, line, 1.0))


def go_directives(pf: ParsedFile) -> GoDirectives:
    """Every directive, struct tag and framework marker read from one Go file."""
    out = GoDirectives()
    if pf.tree is None or pf.lang != "go":
        return out
    source = pf.source
    by_name: dict[str, Symbol] = {
        s.name: s for s in pf.symbols if s.kind == "var" and s.qualified_name.count("::") == 1
    }
    root = pf.tree.root_node
    for n in root.named_children:
        if n.type == "comment":
            m = _GENERATE_RE.match(node_text(n, source).rstrip())
            if m:
                prog = _program_of(m.group(1))
                if prog:
                    line = n.start_point[0] + 1
                    out.facets.append(Facet(FACET_GENERATE, prog, line, line, 1.0))
            continue
        if n.type == "var_declaration":
            _embed_of(n, source, by_name, out)
        elif n.type == "type_declaration":
            _markers(n, source, out.facets)
            for spec in n.named_children:
                if spec.type == "type_spec":
                    name = spec.child_by_field_name("name")
                    _struct_tags(spec, source, node_text(name, source) if name else "", out.facets)
    _cobra_commands(root, source, out.facets)
    del out.facets[_MAX_FACETS_PER_FILE:]
    return out


def _embed_of(decl: Any, source: bytes, by_name: dict[str, Symbol], out: GoDirectives) -> None:
    patterns: list[str] = []
    line = 0
    for cline, text in _comments_above(decl, source):
        m = _EMBED_RE.match(text)
        if m:
            patterns.extend(_split_patterns(m.group(1)))
            line = line or cline
    if not patterns:
        return
    for name in _var_names(decl, source):
        sym = by_name.get(name)
        if sym is None:
            continue
        out.embeds.append(EmbedDirective(sym.qualified_name, sym.id, patterns, line))
        for p in patterns:
            out.facets.append(Facet(FACET_EMBED, p[:200], line, line, 1.0))


def embed_targets(pkg_dir: str, patterns: list[str], live_paths: list[str]) -> list[str]:
    """The live paths the patterns name, Go's `//go:embed` rules."""
    out: list[str] = []
    base = pkg_dir.rstrip("/")
    for raw in patterns:
        include_hidden = raw.startswith("all:")
        pat = raw[4:] if include_hidden else raw
        pat = pat.strip("/")
        if not pat:
            continue
        full = f"{base}/{pat}" if base else pat
        for path in live_paths:
            rel = path[len(base) + 1 :] if base and path.startswith(base + "/") else path
            if rel == pat or path.startswith(full + "/"):
                pass
            elif "/" not in pat and fnmatch.fnmatchcase(rel, pat):
                pass
            elif pat.count("/") == rel.count("/") and fnmatch.fnmatchcase(rel, pat):
                pass
            else:
                continue
            if not include_hidden and any(
                seg.startswith((".", "_")) for seg in rel[len(pat) :].split("/") if seg
            ):
                continue
            out.append(path)
    return sorted(dict.fromkeys(out))


def embed_edges(
    pf: ParsedFile, directives: GoDirectives, live_paths: list[str], edge_cls: Any
) -> list[Any]:
    """`reads` edges var → file pseudo-symbol for every embedded file."""
    if not directives.embeds:
        return []
    pkg_dir = posixpath.dirname(pf.path)
    edges: list[Any] = []
    for d in directives.embeds:
        for target in embed_targets(pkg_dir, d.patterns, live_paths):
            if target == pf.path:
                continue
            edges.append(
                edge_cls(
                    edge_kind="reads",
                    from_symbol_id=d.var_id,
                    to_symbol_id=file_symbol_id(target),
                    from_qualified_name=d.var_qualified_name,
                    to_qualified_name=target,
                    path=pf.path,
                    line=d.line,
                    confidence=1.0,
                )
            )
    return edges


def has_go_facets(pf: ParsedFile) -> bool:
    return bool(go_directives(pf).facets)


__all__ = [
    "GO_FACET_KINDS",
    "EmbedDirective",
    "GoDirectives",
    "embed_edges",
    "embed_targets",
    "go_directives",
    "has_go_facets",
]
