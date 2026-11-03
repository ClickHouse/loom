"""An extractor's response read as untrusted input: `parse_artifact` turns its
JSON into an `Artifact` only when every field has the contract's type and the
parts agree with each other, and refuses it otherwise. Nothing an extractor
returns is chunked, embedded or published before it passes here.

Checked: the size before decoding, the schema version, the format asked for,
each block's type, a unique id and a parent that is an earlier block, at most
MAX_KEYS keys of MAX_KEY_CHARS and MAX_SECTION_DEPTH section entries of
MAX_SECTION_CHARS per block, locator spans that are ranges, coverage counts
within their total, a `complete` flag that matches the counts, strings that are
valid Unicode, numbers that are JSON (no `NaN` or `Infinity`), integers within
64 bits and other numbers within a double, and nesting depth in `attrs` and
`options`. A field the contract does not name is dropped, so an
extractor may add one without a version bump.

Pure and I/O-free.
"""

from __future__ import annotations

import json
import math
from typing import Any, get_args

from server.knowledge.domain.extract.artifact import (
    MAX_KEY_CHARS,
    MAX_KEYS,
    MAX_SECTION_CHARS,
    MAX_SECTION_DEPTH,
    SCHEMA_VERSION,
    Artifact,
    Block,
    BlockType,
    Coverage,
    Failure,
    Locator,
)

_BLOCK_TYPES = frozenset(get_args(BlockType))
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
    )
    if (
        loc.line_start < 0
        or loc.line_end < loc.line_start
        or (loc.line_start == 0) != (loc.line_end == 0)
    ):
        raise _fail(path, "line_start..line_end is not a 1-based range")
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
    """An integer within 64 bits, as a column that stores one holds it; a longer
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
    return Artifact(
        format=fmt,
        parser=parser,
        parser_version=parser_version,
        blocks=tuple(blocks),
        coverage=_coverage(_record(d.get("coverage"), "coverage"), "coverage"),
        warnings=_strs(d, "warnings", "artifact"),
        options=_obj(d, "options", "artifact"),
        attrs=_obj(d, "attrs", "artifact"),
    )
