"""An extractor's response read as untrusted input: `parse_artifact` turns its
JSON into an `Artifact` only when every field has the contract's type and the
parts agree with each other, and refuses it otherwise. Nothing an extractor
returns is chunked, embedded or published before it passes here.

Checked: the size before decoding, the schema version, the format asked for,
each block's type, a unique id and a parent that is an earlier block, each
table's unique id, at most MAX_COLUMNS typed columns of MAX_COLUMN_CHARS and
rows as long as its columns of scalar values or cell errors, at most
MAX_KEYS keys of MAX_KEY_CHARS and MAX_SECTION_DEPTH section entries of
MAX_SECTION_CHARS per block, locator spans that are ranges, coverage counts
within their total, a `complete` flag that matches the counts, strings that are
valid Unicode, numbers that are JSON (no `NaN` or `Infinity`), signed 64-bit
integers and other numbers within a double, and nesting depth in `attrs` and
`options`. A field the contract does not name is dropped, so an
extractor may add one without a version bump.

Pure and I/O-free.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any, get_args

from server.knowledge.domain.extract.artifact import (
    MAX_COLUMN_CHARS,
    MAX_COLUMNS,
    MAX_KEY_CHARS,
    MAX_KEYS,
    MAX_SECTION_CHARS,
    MAX_SECTION_DEPTH,
    SCHEMA_VERSION,
    Artifact,
    Block,
    BlockType,
    Column,
    ColumnType,
    Coverage,
    Failure,
    Locator,
    Table,
)

_BLOCK_TYPES = frozenset(get_args(BlockType))
_COLUMN_TYPES = frozenset(get_args(ColumnType))
# Deeper JSON in `attrs` or `options` is refused before code that walks it sees it.
_MAX_NESTING = 16  # not a setting: a bound on hostile input
_PATH_KEY = 40  # not a setting: the characters of a key an error path names


class InvalidArtifact(ValueError):
    """An extractor's response that is not a contract artifact; the message
    names the first field that failed."""


def _fail(path: str, why: str) -> InvalidArtifact:
    return InvalidArtifact(f"{path}: {why}")


def _str(d: dict[str, Any], key: str, path: str, *, default: str | None = None) -> str:
    v = d.get(key, default)
    if not isinstance(v, str):
        raise _fail(f"{path}.{key}", "is not a string")
    _unicode(v, f"{path}.{key}")
    return v


def _int(d: dict[str, Any], key: str, path: str, *, default: int | None = None) -> int:
    v = d.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool):
        raise _fail(f"{path}.{key}", "is not an integer")
    return v


def _strs(
    d: dict[str, Any], key: str, path: str, *, most: int | None = None, chars: int | None = None
) -> tuple[str, ...]:
    v = d.get(key, [])
    if not isinstance(v, list):
        raise _fail(f"{path}.{key}", "is not a list")
    if most is not None and len(v) > most:
        raise _fail(f"{path}.{key}", f"holds {len(v)}, over the {most} allowed")
    for i, x in enumerate(v):
        if not isinstance(x, str):
            raise _fail(f"{path}.{key}[{i}]", "is not a string")
        if chars is not None and len(x) > chars:
            raise _fail(f"{path}.{key}[{i}]", f"is over the {chars} characters allowed")
        _unicode(x, f"{path}.{key}[{i}]")
    return tuple(v)


def _obj(d: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    v = d.get(key, {})
    if not isinstance(v, dict):
        raise _fail(f"{path}.{key}", "is not an object")
    _json_value(v, f"{path}.{key}", 0)
    return v


def _unicode(s: str, path: str) -> None:
    """A JSON `\\ud800` escape decodes to a lone surrogate, which no UTF-8 column
    or embedder can take."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError as e:
        raise _fail(path, "is not valid Unicode") from e


def _json_value(v: Any, path: str, depth: int) -> None:
    if depth > _MAX_NESTING:
        raise _fail(path, f"nests deeper than {_MAX_NESTING}")
    if isinstance(v, str):
        _unicode(v, path)
    elif isinstance(v, dict):
        for k, x in v.items():
            _unicode(k, path)
            # The key's head only: a path copied per value stays bounded however long
            # the keys an extractor sends.
            _json_value(x, f"{path}.{k[:_PATH_KEY]}", depth + 1)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _json_value(x, f"{path}[{i}]", depth + 1)


def _record(v: Any, path: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise _fail(path, "is not an object")
    return v


def _locator(d: dict[str, Any], path: str) -> Locator:
    loc = Locator(
        line_start=_int(d, "line_start", path, default=0),
        line_end=_int(d, "line_end", path, default=0),
        char_start=_int(d, "char_start", path, default=-1),
        char_end=_int(d, "char_end", path, default=-1),
        key_path=_str(d, "key_path", path, default=""),
        page_start=_int(d, "page_start", path, default=0),
        page_end=_int(d, "page_end", path, default=0),
        sheet=_str(d, "sheet", path, default=""),
        row_start=_int(d, "row_start", path, default=0),
        row_end=_int(d, "row_end", path, default=0),
    )
    if len(loc.sheet) > MAX_SECTION_CHARS:
        raise _fail(f"{path}.sheet", f"is over the {MAX_SECTION_CHARS} characters allowed")
    for name, start, end in (
        ("line", loc.line_start, loc.line_end),
        ("page", loc.page_start, loc.page_end),
        ("row", loc.row_start, loc.row_end),
    ):
        if start < 0 or end < start or (start == 0) != (end == 0):
            raise _fail(path, f"{name}_start..{name}_end is not a 1-based range")
    if (
        loc.char_start < -1
        or loc.char_end < loc.char_start
        or (loc.char_start == -1) != (loc.char_end == -1)
    ):
        raise _fail(path, "char_start..char_end is not a range")
    return loc


def _block(d: dict[str, Any], path: str, seen: set[str]) -> Block:
    block_id = _str(d, "id", path)
    if not block_id or block_id in seen:
        raise _fail(f"{path}.id", "is empty or repeats an earlier block's")
    kind = _str(d, "type", path)
    if kind not in _BLOCK_TYPES:
        raise _fail(f"{path}.type", f"{kind[:40]!r} is not a block type")
    parent = _str(d, "parent", path, default="")
    if parent and parent not in seen:
        raise _fail(f"{path}.parent", "names no earlier block")
    depth = _int(d, "depth", path, default=0)
    if depth < 0:
        raise _fail(f"{path}.depth", "is negative")
    return Block(
        id=block_id,
        type=kind,  # type: ignore[arg-type]  # checked against BlockType above
        text=_str(d, "text", path),
        locator=_locator(_record(d.get("locator", {}), f"{path}.locator"), f"{path}.locator"),
        section_path=_strs(
            d, "section_path", path, most=MAX_SECTION_DEPTH, chars=MAX_SECTION_CHARS
        ),
        parent=parent,
        depth=depth,
        # Once here, so every chunk cut from the block can share the tuple.
        keys=tuple(dict.fromkeys(_strs(d, "keys", path, most=MAX_KEYS, chars=MAX_KEY_CHARS))),
        attrs=_obj(d, "attrs", path),
    )


def _value(v: Any, path: str, i: int, j: int) -> Any:
    """A table value, row `i` column `j`: a JSON scalar, or a cell error
    `{"error": "<text>"}`. Its path is spelled only when it fails."""
    if v is None or isinstance(v, (int, float, bool)):
        return v
    text = v["error"] if isinstance(v, dict) and set(v) == {"error"} else v
    if isinstance(text, str):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as e:
            raise _fail(f"{path}.rows[{i}][{j}]", "is not valid Unicode") from e
        return v
    raise _fail(f"{path}.rows[{i}][{j}]", "is not a scalar or a cell error")


def _table(d: dict[str, Any], path: str) -> Table:
    raw_columns = d.get("columns")
    if not isinstance(raw_columns, list) or len(raw_columns) > MAX_COLUMNS:
        raise _fail(f"{path}.columns", f"is not a list of at most {MAX_COLUMNS}")
    columns = []
    for i, c in enumerate(raw_columns):
        c = _record(c, f"{path}.columns[{i}]")
        name, kind = (
            _str(c, "name", f"{path}.columns[{i}]"),
            _str(c, "type", f"{path}.columns[{i}]"),
        )
        if len(name) > MAX_COLUMN_CHARS:
            raise _fail(f"{path}.columns[{i}].name", f"is over {MAX_COLUMN_CHARS} characters")
        if kind not in _COLUMN_TYPES:
            raise _fail(f"{path}.columns[{i}].type", f"{kind[:40]!r} is not a column type")
        columns.append(Column(name, kind))  # type: ignore[arg-type]  # checked above
    raw_rows = d.get("rows")
    if not isinstance(raw_rows, list):
        raise _fail(f"{path}.rows", "is not a list")
    rows = []
    for i, r in enumerate(raw_rows):
        if not isinstance(r, list) or len(r) != len(columns):
            raise _fail(f"{path}.rows[{i}]", f"is not a list of {len(columns)} values")
        rows.append(tuple(_value(v, path, i, j) for j, v in enumerate(r)))
    header_rows = _int(d, "header_rows", path, default=1)
    if header_rows < 0:
        raise _fail(f"{path}.header_rows", "is negative")
    return Table(
        id=_str(d, "id", path),
        name=_str(d, "name", path),
        columns=tuple(columns),
        rows=tuple(rows),
        header_rows=header_rows,
        locator=_locator(_record(d.get("locator", {}), f"{path}.locator"), f"{path}.locator"),
        attrs=_obj(d, "attrs", path),
    )


def _coverage(d: dict[str, Any], path: str) -> Coverage:
    failed = d.get("failed", [])
    if not isinstance(failed, list):
        raise _fail(f"{path}.failed", "is not a list")
    cov = Coverage(
        unit=_str(d, "unit", path),
        total=_int(d, "total", path),
        extracted=_int(d, "extracted", path),
        failed=tuple(
            Failure(
                _str(_record(f, f"{path}.failed[{i}]"), "unit", f"{path}.failed[{i}]"),
                _str(f, "reason", f"{path}.failed[{i}]"),
            )
            for i, f in enumerate(failed)
        ),
        lost=_strs(d, "lost", path),
    )
    if not cov.unit or not 0 <= cov.extracted <= cov.total:
        raise _fail(path, "extracted is not between 0 and total, or unit is empty")
    # Recomputed, never taken: a partial extraction cannot report itself complete.
    if "complete" in d and d["complete"] is not cov.complete:
        raise _fail(f"{path}.complete", "disagrees with the counts, failures and losses")
    return cov


def _no_constant(name: str) -> Any:
    """`NaN` and `Infinity` are not JSON; the artifact's own bytes must be."""
    raise ValueError(f"{name} is not a JSON number")


def _int64(text: str) -> int:
    """A signed 64-bit integer, as a column that stores one holds it; a longer
    run of digits is not converted at all."""
    if len(text.lstrip("-")) > 19:
        raise ValueError(f"{text[:40]} is out of range")
    value = int(text)
    if not -(2**63) <= value < 2**63:
        raise ValueError(f"{text[:40]} is out of range")
    return value


def _finite(text: str) -> float:
    """A JSON number Python cannot hold (`1e999`) would serialize as `Infinity`."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text[:40]} is out of range")
    return value


def parse_artifact(data: bytes, *, fmt: str, max_bytes: int) -> Artifact:
    """The artifact in `data`, an extractor's response for a `fmt` source, or
    InvalidArtifact. `max_bytes` bounds the response before it is decoded."""
    if len(data) > max_bytes:
        raise _fail("artifact", f"is {len(data)} bytes, over the {max_bytes} allowed")
    try:
        d = json.loads(data, parse_constant=_no_constant, parse_float=_finite, parse_int=_int64)
    except (ValueError, RecursionError) as e:
        raise _fail("artifact", "is not JSON") from e
    d = _record(d, "artifact")
    version = d.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:  # not `true`, not `1.0`
        raise _fail("artifact.schema_version", f"{repr(version)[:40]} is not {SCHEMA_VERSION}")
    if _str(d, "format", "artifact") != fmt:
        raise _fail("artifact.format", f"is not the {fmt!r} asked for")
    parser, parser_version = _str(d, "parser", "artifact"), _str(d, "parser_version", "artifact")
    if not parser or not parser_version:
        raise _fail("artifact", "names no parser or parser_version")
    raw_blocks = d.get("blocks")
    if not isinstance(raw_blocks, list):
        raise _fail("artifact.blocks", "is not a list")
    seen: set[str] = set()
    blocks = []
    for i, b in enumerate(raw_blocks):
        block = _block(_record(b, f"blocks[{i}]"), f"blocks[{i}]", seen)
        seen.add(block.id)
        blocks.append(block)
    raw_tables = d.get("tables", [])
    if not isinstance(raw_tables, list):
        raise _fail("artifact.tables", "is not a list")
    tables = tuple(
        _table(_record(t, f"tables[{i}]"), f"tables[{i}]") for i, t in enumerate(raw_tables)
    )
    if len({t.id for t in tables}) != len(tables) or any(not t.id for t in tables):
        raise _fail("artifact.tables", "holds an empty or repeated id")
    return Artifact(
        format=fmt,
        parser=parser,
        parser_version=parser_version,
        blocks=tuple(blocks),
        coverage=_coverage(_record(d.get("coverage"), "coverage"), "coverage"),
        warnings=_strs(d, "warnings", "artifact"),
        options=_obj(d, "options", "artifact"),
        attrs=_obj(d, "attrs", "artifact"),
        tables=tables,
    )


def refuse_past_source(artifact: Artifact, size: int) -> None:
    """InvalidArtifact when a block or table's line or character range ends past
    a source of `size` bytes. The size bounds both whatever the encoding: each
    character takes at least one byte, and each line but the last ends in one.
    Pages and rows are not bounded: the server does not read the original."""
    _within("blocks", (b.locator for b in artifact.blocks), size)
    _within("tables", (t.locator for t in artifact.tables), size)


def _within(kind: str, locators: Iterable[Locator], size: int) -> None:
    for i, loc in enumerate(locators):
        if loc.char_end > size or loc.line_end > size + 1:
            raise _fail(f"{kind}[{i}].locator", f"ends past the {size}-byte source")
