"""A Prisma schema's models, enums and relations.

`schema.prisma` is the one file that says what a Prisma-backed service's
relational store looks like — langfuse's is 1,944 lines, 74 models, 35
enums — and it is the file every "where is X stored" question ends at. The
grammar is regular enough to read without a tree-sitter package (none is
published for Python): a schema is a sequence of blocks

    model Name {            enum Name {         datasource db { … }
      field Type @attr …      VALUE              generator client { … }
      @@map("table")          @@map("x")
    }                       }

read line by line with `{`/`}` depth, `//` comments dropped.

What the index gets:

* a ``model`` symbol per model (`packages.shared.prisma.schema::Trace`)
  whose signature is the mapped table name (`@@map("traces")`, else the
  model's own name as Prisma would create it), and an ``enum`` symbol per
  enum. ``model`` is a kind the SQL-object gate already knows
  (dbt models): a model binds only from SQL, and a TypeScript
  `prisma.trace.findMany()` is a call to the client, not to the table;
* a ``creates`` facet per model naming the mapped TABLE, so
  `code.tests_for({facets:[{kind:"creates",name:"traces"}]})` answers with
  the schema beside the migration that created it;
* ``uses_type`` edges from a model to every model or enum a field's type
  names (the relation graph, resolved inside the one file);
* each block is its own chunk (the generic container chunking).

Outside the extractor stamp with the other lane modules: a fix here is one
`loom code backfill --paths-file` over the schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from server.code.domain.extract.chunker import Chunk, _assign_ordinals
from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.lang_sql_objects import FACET_CREATES
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_PRISMA = "prisma"
KIND_MODEL = "model"
EDGE_USES_TYPE = "uses_type"

_BLOCK_RE = re.compile(r"^\s*(model|enum|type|view|datasource|generator)\s+(\w+)\s*\{")
_MAP_RE = re.compile(r"""@@map\(\s*(?:name\s*:\s*)?["']([^"']+)["']""")
_FIELD_RE = re.compile(r"^\s*(\w+)\s+(\w+)(\[\])?\??")
_SCALARS: frozenset[str] = frozenset(
    {
        "String",
        "Int",
        "BigInt",
        "Float",
        "Decimal",
        "Boolean",
        "DateTime",
        "Json",
        "Bytes",
        "Unsupported",
    }
)


@dataclass
class Block:
    kind: str
    name: str
    start_line: int
    end_line: int
    lines: list[str] = field(default_factory=list)  # body lines, comments stripped
    doc: str = ""

    def mapped_name(self) -> str:
        for ln in self.lines:
            m = _MAP_RE.search(ln)
            if m:
                return m.group(1)
        return self.name

    def field_types(self) -> list[tuple[str, str, int]]:
        """(field, type, line) per field line of a model."""
        out: list[tuple[str, str, int]] = []
        for i, ln in enumerate(self.lines):
            s = ln.strip()
            if not s or s.startswith("@@"):
                continue
            m = _FIELD_RE.match(ln)
            if m:
                out.append((m.group(1), m.group(2), self.start_line + 1 + i))
        return out


def _strip_comment(line: str) -> str:
    i = line.find("//")
    return line if i < 0 else line[:i]


def blocks(text: str) -> list[Block]:
    """The schema's top-level blocks with their doc comments (`///` or `//`
    lines right above)."""
    out: list[Block] = []
    lines = text.splitlines()
    current: Block | None = None
    depth = 0
    doc: list[str] = []
    for i, raw in enumerate(lines, start=1):
        if current is None:
            m = _BLOCK_RE.match(raw)
            if m:
                current = Block(m.group(1), m.group(2), i, i, doc="\n".join(doc)[:2000])
                depth = raw.count("{") - raw.count("}")
                doc = []
                if depth <= 0:
                    current.end_line = i
                    out.append(current)
                    current = None
                continue
            s = raw.strip()
            if s.startswith("//"):
                doc.append(s)
            elif s:
                doc = []
            continue
        code = _strip_comment(raw)
        depth += code.count("{") - code.count("}")
        if depth <= 0:
            current.end_line = i
            out.append(current)
            current = None
        else:
            current.lines.append(code)
    return out


@dataclass
class PrismaUnits:
    symbols: list[Symbol]
    facets: list[Facet]
    edges: list[tuple[str, str, str, int]]  # (from qname, to qname, kind, line)


def prisma_units(path: str, text: str) -> PrismaUnits:
    mod = module_scope(path)
    syms: list[Symbol] = []
    facets: list[Facet] = []
    by_name: dict[str, str] = {}
    blks = blocks(text)
    for b in blks:
        if b.kind not in {"model", "enum", "type", "view"}:
            continue
        kind = "enum" if b.kind == "enum" else KIND_MODEL
        qname = f"{mod}::{b.name}"
        mapped = b.mapped_name() if kind == KIND_MODEL else ""
        syms.append(
            Symbol(
                qualified_name=qname,
                name=b.name,
                kind=kind,
                signature=mapped,
                path=path,
                start_line=b.start_line,
                end_line=b.end_line,
                doc_comment=b.doc,
            )
        )
        by_name[b.name] = qname
        if kind == KIND_MODEL and b.kind != "type":
            facets.append(Facet(FACET_CREATES, mapped, len(syms), b.start_line, 1.0))
    edges: list[tuple[str, str, str, int]] = []
    for b in blks:
        if b.kind not in {"model", "type", "view"}:
            continue
        seen: set[str] = set()
        for _f, typ, line in b.field_types():
            if typ in _SCALARS or typ == b.name or typ not in by_name or typ in seen:
                continue
            seen.add(typ)
            edges.append((f"{mod}::{b.name}", by_name[typ], EDGE_USES_TYPE, line))
    return PrismaUnits(symbols=syms, facets=facets, edges=edges)


def parse_prisma(path: str, source: bytes) -> ParsedFile:
    pf = ParsedFile(path=path, lang=LANG_PRISMA, tree=None, source=source)
    pf.symbols = prisma_units(path, source.decode("utf-8", "replace")).symbols
    return pf


__all__ = [
    "EDGE_USES_TYPE",
    "KIND_MODEL",
    "LANG_PRISMA",
    "Block",
    "PrismaUnits",
    "blocks",
    "parse_prisma",
    "prisma_chunks",
    "prisma_edges",
    "prisma_units",
]


def prisma_chunks(pf: ParsedFile) -> list[Chunk]:
    """One chunk per block: a model or enum (with its doc comment) as a
    `class_skeleton` carrying its symbol id, a datasource / generator block
    as `other`. Text between blocks is comments and blank lines and rides
    the block below it as the doc; nothing else is dropped."""
    text = pf.source.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    by_span = {(s.start_line, s.end_line): s for s in pf.symbols}
    out: list[Chunk] = []
    for b in blocks(text):
        body = "".join(lines[b.start_line - 1 : b.end_line])
        sym = by_span.get((b.start_line, b.end_line))
        content = (b.doc + "\n" if b.doc else "") + body
        out.append(
            Chunk(
                path=pf.path,
                scope=sym.qualified_name if sym else "",
                kind="class_skeleton" if sym else "other",
                start_line=b.start_line,
                end_line=b.end_line,
                content=content,
                symbol_ids=[sym.id] if sym else [],
            )
        )
    _assign_ordinals(out)
    return out


def prisma_edges(pf: ParsedFile) -> list[Edge]:
    """`uses_type` edges of the relation graph, model → model / enum, all
    inside the one schema file (no name table: the schema defines every
    type it names)."""
    text = pf.source.decode("utf-8", "replace")
    by_qname = {s.qualified_name: s for s in pf.symbols}
    out: list[Edge] = []
    for from_q, to_q, kind, line in prisma_units(pf.path, text).edges:
        a, b = by_qname.get(from_q), by_qname.get(to_q)
        if a is None or b is None:
            continue
        out.append(Edge(kind, a.id, b.id, a.qualified_name, b.qualified_name, pf.path, line, 1.0))
    return out
