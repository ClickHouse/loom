"""FlatBuffers schemas: tables, structs, enums, unions, services.

A `.fbs` file is the wire and on-disk contract of a storage engine — its
schemas (`src/proto/log-store.fbs`, `page-store.fbs`, …) say what a
change-record, a page header or a coordination message holds,
and the generated C++ (`*_generated.h`, excluded as generated) is where the
code reads them. The grammar is regular enough to read without a
tree-sitter package: top-level blocks

    namespace engine.proto;        include "header.fbs";
    table Name (attrs) { field: type (attrs); … }
    struct Name { … }              enum Name : ubyte { A, B = 2 }
    union Name { A, B }            rpc_service Name { Method(Req): Resp; }
    root_type Name;                file_identifier "ENGN";

with `//` comments and `/// doc` comments. What the index gets:

* a symbol per block — kind ``struct`` for `table` and `struct` (the
  signature says which), ``enum``, ``union``, ``service`` for `rpc_service`,
  and a ``function`` per rpc method under its service; qualified names are
  `<namespace>::<Name>` with the schema's `namespace a.b` read as `a::b`;
* `include "x.fbs"` → include targets, resolved against the namespace's
  paths like a C++ include (the includer's directory first);
* ``uses_type`` edges from a table / struct / union / method to every
  schema type a field, union member, request or response names — in-file
  by name here, cross-file through the namespace name table
  (`ingest._fbs_reference_edges`);
* a ``root_type`` facet on the file naming the root table, and a
  ``file_identifier`` facet when declared;
* one chunk per block (`class_skeleton`), the leading namespace / include /
  attribute lines as `file_header`.

Outside the extractor stamp with the other lane modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.chunker import Chunk, _assign_ordinals
from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_FBS = "flatbuffers"
KIND_SERVICE = "service"
KIND_UNION = "union"
FACET_ROOT_TYPE = "root_type"
FACET_FILE_IDENTIFIER = "file_identifier"
EDGE_USES_TYPE = "uses_type"

_SCALARS: frozenset[str] = frozenset(
    {
        "bool",
        "byte",
        "ubyte",
        "int8",
        "uint8",
        "short",
        "ushort",
        "int16",
        "uint16",
        "int",
        "uint",
        "int32",
        "uint32",
        "long",
        "ulong",
        "int64",
        "uint64",
        "float",
        "float32",
        "double",
        "float64",
        "string",
    }
)
_BLOCK_RE = re.compile(
    r"^\s*(table|struct|enum|union|rpc_service)\s+([A-Za-z_]\w*)\s*(?::\s*([A-Za-z_]\w*))?\s*(\([^)]*\))?\s*\{"
)
_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.]*)\s*;")
_INCLUDE_RE = re.compile(r'^\s*include\s+"([^"]+)"\s*;')
_ROOT_RE = re.compile(r"^\s*root_type\s+([A-Za-z_][\w.]*)\s*;")
_FILEID_RE = re.compile(r'^\s*file_identifier\s+"([^"]+)"\s*;')
_FIELD_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*(\[?)([A-Za-z_][\w.]*)(?::\s*\d+)?\]?")
# Each optional part owns the blanks before it, so a blank run is read one way only.
_UNION_MEMBER_RE = re.compile(
    r"^\s*([A-Za-z_][\w.]*)(?:\s*:\s*([A-Za-z_][\w.]*))?(?:\s*=\s*\d+)?\s*(?:,\s*)?$"
)
_RPC_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*\(\s*([A-Za-z_][\w.]*)\s*\)\s*:\s*([A-Za-z_][\w.]*)")


@dataclass
class Block:
    kind: str  # table | struct | enum | union | rpc_service
    name: str
    start_line: int
    end_line: int
    base: str = ""  # enum underlying type
    attrs: str = ""
    lines: list[str] = field(default_factory=list)  # body lines, comments stripped
    doc: str = ""


def _strip_comment(line: str) -> str:
    i = line.find("//")
    return line if i < 0 else line[:i]


@dataclass
class FbsUnits:
    namespace: str
    symbols: list[Symbol]
    facets: list[Facet]
    includes: list[str]
    refs: list[tuple[str, str, int]]  # (from qualified name, referenced type as written, line)
    header_end: int
    blocks: list[Block]


def _scope(namespace: str) -> str:
    return namespace.replace(".", "::")


def _file_level(raw: str, line: int, state: dict[str, Any]) -> bool:
    """A top-level declaration line (namespace / include / root_type /
    file_identifier) recorded into `state`; True when the line was one."""
    m = _NAMESPACE_RE.match(raw)
    if m:
        state["namespace"] = m.group(1)
        return True
    m = _INCLUDE_RE.match(raw)
    if m:
        state["includes"].append(m.group(1))
        return True
    m = _ROOT_RE.match(raw)
    if m:
        state["decls"].append((FACET_ROOT_TYPE, m.group(1), line))
        return True
    m = _FILEID_RE.match(raw)
    if m:
        state["decls"].append((FACET_FILE_IDENTIFIER, m.group(1), line))
        return True
    return False


def _open_block(raw: str, m: re.Match[str], line: int, doc: list[str]) -> tuple[Block, int]:
    """The block a `table X {` line opens and the brace depth after it; a
    block that closes on the same line keeps its inner text as its body."""
    block = Block(
        m.group(1),
        m.group(2),
        line,
        line,
        base=m.group(3) or "",
        attrs=(m.group(4) or "").strip("()"),
        doc="\n".join(doc)[:2000],
    )
    rest = _strip_comment(raw[m.end() :])
    depth = raw.count("{") - raw.count("}")
    if depth <= 0:
        inner = rest[: rest.rfind("}")] if "}" in rest else rest
        if inner.strip():
            block.lines.append(inner)
        return block, 0
    if rest.strip():
        block.lines.append(rest)
    return block, depth


def blocks_of(text: str) -> tuple[list[Block], str, list[str], list[tuple[str, str, int]]]:
    """(blocks, namespace, includes, file-level declarations) — declarations
    are (kind, value, line) for root_type / file_identifier."""
    state: dict[str, Any] = {"namespace": "", "includes": [], "decls": []}
    out: list[Block] = []
    current: Block | None = None
    depth = 0
    doc: list[str] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        if current is None:
            if _file_level(raw, i, state):
                continue
            m = _BLOCK_RE.match(raw)
            if m:
                current, depth = _open_block(raw, m, i, doc)
                doc = []
                if depth <= 0:
                    out.append(current)
                    current = None
                continue
            s_ = raw.strip()
            if s_.startswith("///"):
                doc.append(s_[3:].strip())
            elif s_.startswith("//"):
                doc.append(s_[2:].strip())
            elif s_:
                doc = []
            continue
        code = _strip_comment(raw)
        depth += code.count("{") - code.count("}")
        if depth <= 0:
            tail = code[: code.rfind("}")] if "}" in code else code
            if tail.strip():
                current.lines.append(tail)
            current.end_line = i
            out.append(current)
            current = None
        else:
            current.lines.append(code)
    return out, state["namespace"], state["includes"], state["decls"]


def _bare(t: str) -> str:
    return t.rsplit(".", 1)[-1]


def fbs_units(path: str, text: str) -> FbsUnits:
    blks, namespace, includes, decls = blocks_of(text)
    scope = _scope(namespace)
    syms: list[Symbol] = []
    facets: list[Facet] = []
    refs: list[tuple[str, str, int]] = []
    q = (lambda n: f"{scope}::{n}") if scope else (lambda n: n)
    for b in blks:
        if b.kind in ("table", "struct"):
            kind, sig = "struct", b.kind + (f" ({b.attrs})" if b.attrs else "")
        elif b.kind == "enum":
            kind, sig = "enum", f"enum : {b.base}" if b.base else "enum"
        elif b.kind == "union":
            kind, sig = KIND_UNION, "union"
        else:
            kind, sig = KIND_SERVICE, "rpc_service"
        syms.append(
            Symbol(q(b.name), b.name, kind, sig, path, b.start_line, b.end_line, doc_comment=b.doc)
        )
        if b.kind in ("table", "struct"):
            for n, ln in enumerate(b.lines, start=1):
                m = _FIELD_RE.match(ln)
                if m and _bare(m.group(3)) not in _SCALARS:
                    refs.append((q(b.name), m.group(3), b.start_line + n))
        elif b.kind == "union":
            for n, ln in enumerate(b.lines, start=1):
                line = b.start_line if b.end_line == b.start_line else b.start_line + n
                for member in ln.split(","):
                    m = _UNION_MEMBER_RE.match(member.strip())
                    if m:
                        typ = m.group(2) or m.group(1)
                        if _bare(typ) not in _SCALARS:
                            refs.append((q(b.name), typ, line))
        else:
            for n, ln in enumerate(b.lines, start=1):
                m = _RPC_RE.match(ln)
                if m:
                    mq = f"{q(b.name)}::{m.group(1)}"
                    syms.append(
                        Symbol(
                            mq,
                            m.group(1),
                            "function",
                            f"({m.group(2)}): {m.group(3)}",
                            path,
                            b.start_line + n,
                            b.start_line + n,
                        )
                    )
                    refs.append((mq, m.group(2), b.start_line + n))
                    refs.append((mq, m.group(3), b.start_line + n))
    for kind, value, line in decls:
        facets.append(Facet(kind, _bare(value) if kind == FACET_ROOT_TYPE else value, 0, line, 1.0))
    header_end = min((b.start_line for b in blks), default=len(text.splitlines()) + 1) - 1
    return FbsUnits(namespace, syms, facets, includes, refs, max(header_end, 0), blks)


def parse_fbs(path: str, source: bytes) -> ParsedFile:
    pf = ParsedFile(path=path, lang=LANG_FBS, tree=None, source=source)
    units = fbs_units(path, source.decode("utf-8", "replace"))
    pf.symbols = units.symbols
    pf.includes = units.includes
    return pf


def fbs_chunks(pf: ParsedFile) -> list[Chunk]:
    text = pf.source.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    units = fbs_units(pf.path, text)
    by_span = {(s.start_line, s.end_line): s for s in pf.symbols if s.kind != "function"}
    out: list[Chunk] = []
    if units.header_end >= 1 and "".join(lines[: units.header_end]).strip():
        out.append(
            Chunk(
                pf.path, "", "file_header", 1, units.header_end, "".join(lines[: units.header_end])
            )
        )
    cursor = units.header_end + 1
    for b in units.blocks:
        if b.start_line > cursor:
            gap = "".join(lines[cursor - 1 : b.start_line - 1])
            if gap.strip():
                out.append(Chunk(pf.path, "", "other", cursor, b.start_line - 1, gap))
        sym = by_span.get((b.start_line, b.end_line))
        body = "".join(lines[b.start_line - 1 : b.end_line])
        content = (b.doc + "\n" if b.doc else "") + body
        methods = [
            s.id
            for s in pf.symbols
            if s.kind == "function" and b.start_line <= s.start_line <= b.end_line
        ]
        out.append(
            Chunk(
                pf.path,
                sym.qualified_name if sym else "",
                "class_skeleton" if sym else "other",
                b.start_line,
                b.end_line,
                content,
                ([sym.id] if sym else []) + methods,
            )
        )
        cursor = b.end_line + 1
    if cursor <= len(lines) and "".join(lines[cursor - 1 :]).strip():
        out.append(Chunk(pf.path, "", "other", cursor, len(lines), "".join(lines[cursor - 1 :])))
    _assign_ordinals(out)
    return out


def fbs_local_edges(pf: ParsedFile) -> tuple[list[Edge], list[tuple[str, str, int]]]:
    """In-file `uses_type` edges plus the references to types this file does
    not define (an included schema's)."""
    units = fbs_units(pf.path, pf.source.decode("utf-8", "replace"))
    by_q = {s.qualified_name: s for s in pf.symbols}
    by_name = {s.name: s for s in pf.symbols if s.kind != "function"}
    edges: list[Edge] = []
    external: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for from_q, typ, line in units.refs:
        a = by_q.get(from_q)
        if a is None or (from_q, typ) in seen:
            continue
        seen.add((from_q, typ))
        b = by_q.get(_scope(typ)) or by_name.get(_bare(typ))
        if b is None or b.qualified_name == from_q:
            if b is None:
                external.append((from_q, _bare(typ), line))
            continue
        edges.append(
            Edge(EDGE_USES_TYPE, a.id, b.id, a.qualified_name, b.qualified_name, pf.path, line, 1.0)
        )
    return edges, external


def fbs_external_edges(
    pf: ParsedFile,
    external: list[tuple[str, str, int]],
    table: dict[str, list[tuple[str, str, str]]],
) -> list[Edge]:
    """`uses_type` edges to schema types defined in an included `.fbs`,
    through the name table (bare name → [(symbol_id, qualified name,
    kind)]), kept when exactly one FlatBuffers type candidate exists."""
    by_q = {s.qualified_name: s for s in pf.symbols}
    out: list[Edge] = []
    for from_q, name, line in external:
        a = by_q.get(from_q)
        cands = [c for c in table.get(name, ()) if c[2] in ("struct", "enum", KIND_UNION)]
        if a is None or len(cands) != 1:
            continue
        sid, qname, _k = cands[0]
        out.append(Edge(EDGE_USES_TYPE, a.id, sid, a.qualified_name, qname, pf.path, line, 0.9))
    return out


__all__ = [
    "EDGE_USES_TYPE",
    "FACET_FILE_IDENTIFIER",
    "FACET_ROOT_TYPE",
    "KIND_SERVICE",
    "KIND_UNION",
    "LANG_FBS",
    "Block",
    "FbsUnits",
    "blocks_of",
    "fbs_chunks",
    "fbs_external_edges",
    "fbs_local_edges",
    "fbs_units",
    "parse_fbs",
]
