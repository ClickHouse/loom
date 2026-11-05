"""The extraction artifact a parser builds: version 1 of the wire contract the
Loom server reads (docs/reference/extractor-contract.md). The server keeps its
own reader and checks every field; this side only writes.

Every format is parsed into the same shape, so chunking, search, outline and
citation read one contract whatever the file was. An artifact is ordered
blocks (a heading, a paragraph, a SQL statement, a TOML key, ...), each with
its section context and a locator back into the original, plus a coverage
record and warnings.

An artifact is immutable and addressed by the sha256 of its JSON: a new parser
version makes a new artifact (a new extraction generation) of the same source
revision, never a changed one.

Pure and I/O-free.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import platform
import re
from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal

# Bumped when the JSON shape changes in a way an older reader cannot read. An
# added field needs no bump: a reader keeps the fields it knows and drops the rest.
SCHEMA_VERSION = 1
# The contract's bounds on a block, which the server refuses past: its exact keys
# and each key's length, its section path's entries and each entry's length.
MAX_KEYS = 10_000
MAX_KEY_CHARS = 1024
MAX_SECTION_DEPTH = 64
MAX_SECTION_CHARS = 500
# And on a table: its columns (a spreadsheet's widest sheet) and each column name.
MAX_COLUMNS = 16_384
MAX_COLUMN_CHARS = 500

BlockType = Literal[
    "heading",
    "paragraph",
    "list",
    "code",
    "sql_statement",
    "toml_table",
    "toml_key",
]


@dataclass(frozen=True)
class Locator:
    """Where a block sits in the original revision. Only the fields that apply
    to the format are set; `to_dict` drops the rest."""

    line_start: int = 0  # 1-based, inclusive
    line_end: int = 0
    char_start: int = -1  # 0-based offset into the decoded text
    char_end: int = -1  # exclusive
    key_path: str = ""  # TOML `server.limits.max_rows`
    page_start: int = 0  # a PDF's pages or a PPTX's slides, 1-based, inclusive
    page_end: int = 0
    sheet: str = ""  # an XLSX sheet's name
    # A sheet's or a CSV file's rows, 1-based, inclusive, the header counted; a
    # Parquet file's rows from 1 (it has no header row).
    row_start: int = 0
    row_end: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) != f.default
        }


@dataclass(frozen=True)
class Block:
    """One unit of source structure. `id` is stable within the artifact
    (`b<ordinal>`); `parent` names the enclosing block (a heading, a TOML
    table), `section_path` the headings above it. `keys` are exact identifiers
    a reader may look the block up by (SQL object names, TOML key paths)."""

    id: str
    type: BlockType
    text: str
    locator: Locator = field(default_factory=Locator)
    section_path: tuple[str, ...] = ()
    parent: str = ""
    depth: int = 0
    keys: tuple[str, ...] = ()
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type, "text": self.text}
        loc = self.locator.to_dict()
        if loc:
            out["locator"] = loc
        if self.section_path:
            out["section_path"] = list(self.section_path)
        if self.parent:
            out["parent"] = self.parent
        if self.depth:
            out["depth"] = self.depth
        if self.keys:
            out["keys"] = list(self.keys)
        if self.attrs:
            out["attrs"] = self.attrs
        return out


@dataclass(frozen=True)
class Failure:
    """A unit (a statement's block, a TOML key, a line, the file) the parser
    could not extract, and why."""

    unit: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"unit": self.unit, "reason": self.reason}


@dataclass(frozen=True)
class Coverage:
    """How much of the source made it into blocks. `unit` is what the format is
    made of (`lines`, `statements`, `keys`, `chars`), `total` how many there
    are and `extracted` how many made it; `failed` names each unit that did
    not. `lost` lists capabilities the artifact cannot serve for this source
    (`sql_structure` for an unparsed statement), so a partial extraction is
    never reported as complete."""

    unit: str
    total: int
    extracted: int
    failed: tuple[Failure, ...] = ()
    lost: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.extracted == self.total and not self.failed and not self.lost

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "total": self.total,
            "extracted": self.extracted,
            "failed": [f.to_dict() for f in self.failed],
            "lost": list(self.lost),
            "complete": self.complete,
        }


# The type of a table's column: what every value it holds is, `mixed` when they differ.
ColumnType = Literal["string", "number", "bool", "date", "datetime", "time", "duration", "mixed"]


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type}


@dataclass(frozen=True)
class Table:
    """A sheet's, a CSV file's or a Parquet file's rows as values: each row a
    list as long as `columns`, each value a JSON scalar (a date as ISO text, a
    cell error as `{"error": "#DIV/0!"}`). `locator` names its sheet and the
    rows its values came from; `header_rows` how many rows above them named the
    columns (0 for a Parquet schema, whose names are no row)."""

    id: str
    name: str
    columns: tuple[Column, ...]
    rows: tuple[tuple[Any, ...], ...]
    header_rows: int = 1
    locator: Locator = field(default_factory=Locator)
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "columns": [c.to_dict() for c in self.columns],
            "rows": [list(r) for r in self.rows],
            "header_rows": self.header_rows,
        }
        loc = self.locator.to_dict()
        if loc:
            out["locator"] = loc
        if self.attrs:
            out["attrs"] = self.attrs
        return out


@dataclass(frozen=True)
class Artifact:
    """A source revision's normalized extraction. `parser` and `parser_version`
    name what made it; `options` the overrides it ran with (an SQL dialect, an
    encoding); `attrs` format facts a reader needs (the detected dialect and
    how it was chosen)."""

    format: str
    parser: str
    parser_version: str
    blocks: tuple[Block, ...]
    coverage: Coverage
    warnings: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)
    # A tabular source's values, beside the blocks its rows are searched by.
    tables: tuple[Table, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "format": self.format,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "options": self.options,
            "attrs": self.attrs,
            "coverage": self.coverage.to_dict(),
            "warnings": list(self.warnings),
            "blocks": [b.to_dict() for b in self.blocks],
        }
        if self.tables:
            out["tables"] = [t.to_dict() for t in self.tables]
        return out

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json()).hexdigest()


class Lines:
    """Line numbers of offsets into one text and back, from its newline
    offsets: built once per text, so locating every block is linear."""

    def __init__(self, text: str) -> None:
        self._newlines = [m.start() for m in re.finditer("\n", text)]

    def of(self, offset: int) -> int:
        """The 1-based line holding char `offset`."""
        return bisect.bisect_left(self._newlines, offset) + 1

    def start(self, index: int) -> int:
        """The offset where the 0-based line `index` starts."""
        return self._newlines[index - 1] + 1 if index else 0


class Blocks:
    """Accumulates blocks with a heading stack, giving each block its section
    path and the id of the heading it sits under."""

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self._stack: list[tuple[int, str, str]] = []  # (depth, title, block id)

    def heading(self, depth: int, title: str, loc: Locator, **attrs: object) -> None:
        while self._stack and self._stack[-1][0] >= depth:
            self._stack.pop()
        parent = self._stack[-1][2] if self._stack else ""
        bid = f"b{len(self.blocks)}"
        # The whole title: `within_contract` cuts a path entry to the contract's bound
        # and marks the cut. The heading block keeps all of it, and a long one is
        # chunked like any long block.
        self._stack.append((depth, title, bid))
        self.blocks.append(
            Block(
                id=bid,
                type="heading",
                text=title,
                locator=loc,
                section_path=tuple(t for _, t, _ in self._stack),
                parent=parent,
                depth=depth,
                attrs=dict(attrs),
            )
        )

    def close_sections(self) -> None:
        """The blocks added next sit under no heading (a slide with no title)."""
        self._stack.clear()

    def add(self, type_: BlockType, text: str, loc: Locator, **attrs: object) -> None:
        self.blocks.append(
            Block(
                id=f"b{len(self.blocks)}",
                type=type_,
                text=text,
                locator=loc,
                section_path=tuple(t for _, t, _ in self._stack),
                parent=self._stack[-1][2] if self._stack else "",
                attrs=dict(attrs),
            )
        )


def on_python(version: str) -> str:
    """A parser version that names the Python it ran on: for a parser that ships
    with Python (`html.parser`, `tomllib`), whose output can change with it."""
    return f"{version}+python{platform.python_version()}"


def within_contract(artifact: Artifact) -> Artifact:
    """`artifact` with each block cut to the contract's bounds: a key longer than
    MAX_KEY_CHARS is left out and the first MAX_KEYS kept (`attrs.keys_truncated`,
    and `exact_keys` in `coverage.lost`), and the section path keeps its first
    MAX_SECTION_DEPTH entries, each cut to MAX_SECTION_CHARS (`attrs.section_truncated`,
    and `section_path` lost). The block's text and locator still hold all of it."""
    blocks = []
    lost = list(artifact.coverage.lost)
    for b in artifact.blocks:
        keys = tuple(k for k in b.keys if len(k) <= MAX_KEY_CHARS)[:MAX_KEYS]
        section = tuple(s[:MAX_SECTION_CHARS] for s in b.section_path[:MAX_SECTION_DEPTH])
        if keys == b.keys and section == b.section_path:
            blocks.append(b)
            continue
        attrs = dict(b.attrs)
        for cut, flag, loss in (
            (keys != b.keys, "keys_truncated", "exact_keys"),
            (section != b.section_path, "section_truncated", "section_path"),
        ):
            if cut:
                attrs[flag] = True
                if loss not in lost:
                    lost.append(loss)
        blocks.append(replace(b, keys=keys, section_path=section, attrs=attrs))
    coverage = replace(artifact.coverage, lost=tuple(lost))
    return replace(artifact, blocks=tuple(blocks), coverage=coverage)
