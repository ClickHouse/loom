"""TOML into one block per table header and per key, with full key paths.

`tomllib` decides what the file means (types, values, validity); a line
scanner beside it finds where each table and key sits, the comments above and
beside it, and the original text of its value. A key block's text is written
as `<full path> = <value text>` with its comments, so it reads alone; its
line span is what points into the source. A key's path is complete from
the root, arrays of tables numbered (`retention.rules[1].days`), and both that
path and its index-free form (`retention.rules.days`) are the block's `keys`,
so an exact lookup by either finds it.

An invalid file is still searchable as text; its loss of types is recorded in
coverage (`toml_types`) with the reason, which names the line or the key path
where it can.

Pure and I/O-free.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
import tomllib
from dataclasses import dataclass
from typing import Any

from loom_extractor.artifact import (
    Artifact,
    Block,
    Coverage,
    Failure,
    Locator,
    on_python,
)
from loom_extractor.text import paragraph_blocks

# Bumped whenever this parser's output changes, including an invalid file's
# paragraphs, which `text.paragraph_blocks` builds.
PARSER_VERSION = "1"
# Arrays and inline tables nested deeper than this keep their text and type but
# no `attrs.value`, so the artifact stays inside the nesting the server reads.
_VALUE_DEPTH = 8


def _deeper_than(value: Any, limit: int) -> bool:
    """Whether `value` nests arrays or tables more than `limit` deep, read one
    level at a time (no recursion on a hostile value)."""
    level = [value]
    for _ in range(limit + 1):
        level = [
            x
            for v in level
            if isinstance(v, (dict, list))
            for x in (v.values() if isinstance(v, dict) else v)
        ]
        if not level:
            return False
    return True


_BARE = re.compile(r"[A-Za-z0-9_-]+")


def _read_key(s: str, i: int) -> tuple[list[str], int] | None:
    """A dotted key starting at `s[i]`: (its parts, the index after it), or None."""
    parts: list[str] = []
    while True:
        while i < len(s) and s[i] in " \t":
            i += 1
        if i >= len(s):
            return None
        if s[i] == '"':
            j = i + 1
            while j < len(s) and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= len(s):
                return None
            # tomllib decodes the key's escapes (\uXXXX among them) as the file means them.
            try:
                parts.append(next(iter(tomllib.loads(s[i : j + 1] + " = 0"))))
            except tomllib.TOMLDecodeError:
                return None
            i = j + 1
        elif s[i] == "'":
            j = s.find("'", i + 1)
            if j < 0:
                return None
            parts.append(s[i + 1 : j])
            i = j + 1
        else:
            m = _BARE.match(s, i)
            if not m:
                return None
            parts.append(m.group(0))
            i = m.end()
        while i < len(s) and s[i] in " \t":
            i += 1
        if i < len(s) and s[i] == ".":
            i += 1
            continue
        return parts, i


def _value_end(lines: list[str], row: int, col: int) -> tuple[int, str, str]:
    """Scan a value from `lines[row][col]`: (its last row, its text, the
    comment after it). Strings, arrays and inline tables may span rows."""
    depth = 0
    quote = ""  # the open string's delimiter
    out: list[str] = []
    r, c = row, col
    while r < len(lines):
        line = lines[r]
        while c < len(line):
            ch = line[c]
            if quote:
                if quote in ('"', '"""') and ch == "\\":
                    out.append(line[c : c + 2])
                    c += 2
                    continue
                if line.startswith(quote, c):
                    # `"""x""""` closes on its last three quotes: up to two before
                    # them are the string's own.
                    extra = 0
                    if len(quote) == 3:
                        while extra < 2 and line.startswith(quote[0], c + 3 + extra):
                            extra += 1
                    out.append(line[c : c + 3 + extra] if len(quote) == 3 else quote)
                    c += len(quote) + extra
                    quote = ""
                    continue
                out.append(ch)
                c += 1
                continue
            if ch == "#":
                if depth == 0:
                    return r, "".join(out).strip(), line[c + 1 :].strip()
                out.append(line[c:])  # a comment inside a multi-row array stays in its text
                c = len(line)
                break
            for q in ('"""', "'''", '"', "'"):
                if line.startswith(q, c):
                    quote = q
                    out.append(q)
                    c += len(q)
                    break
            else:
                if ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                out.append(ch)
                c += 1
        if depth <= 0 and quote not in ('"""', "'''"):
            return r, "".join(out).strip(), ""
        out.append("\n")
        r, c = r + 1, 0
    return len(lines) - 1, "".join(out).strip(), ""


def _type_of(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, dt.datetime):
        return "datetime" if v.tzinfo else "local_datetime"
    if isinstance(v, dt.date):
        return "date"
    if isinstance(v, dt.time):
        return "time"
    if isinstance(v, list):
        return "array"
    return "table"


def _jsonable(v: Any) -> Any:
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


@dataclass(frozen=True)
class _Step:
    name: str
    index: int = -1  # the element of an array of tables


def _path_text(steps: list[_Step], *, indexed: bool = True) -> str:
    """The key path as TOML spells it: a key that is not bare is a basic string
    (JSON's escapes are TOML's, and DEL, which JSON leaves as is, is escaped
    too), and an array of tables' element is `[n]` unless `indexed` is off."""

    def part(s: str) -> str:
        return (
            s
            if _BARE.fullmatch(s)
            else json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")
        )

    return ".".join(
        part(s.name) + (f"[{s.index}]" if indexed and s.index >= 0 else "") for s in steps
    )


def _lookup(doc: Any, steps: list[_Step]) -> Any:
    cur = doc
    for s in steps:
        cur = cur[s.name]
        if s.index >= 0:
            cur = cur[s.index]
    return cur


class _Scanner:
    """Walks the lines once, keeping the current table and the comments that
    precede the next header or key."""

    def __init__(self, doc: dict[str, Any], lines: list[str]) -> None:
        self.doc = doc
        self.lines = lines
        self.blocks: list[Block] = []
        self.table: list[_Step] = []
        self.table_id = ""
        # A path is named by an id built one part at a time from its parent's id, so
        # a header of n parts costs n lookups, not n copies of its prefixes.
        self.path_ids: dict[tuple[int, str], int] = {}
        self.aot_counts: dict[int, int] = {}
        # Every array-of-tables path under each path, so a new element resets
        # only its own nested numbering.
        self.aot_under: dict[int, set[int]] = {}
        self.comments: list[str] = []
        self.comment_row = 0  # the row of the first comment in `comments`
        self.unplaced: list[int] = []  # 1-based lines the scanner could not read

    def _first_row(self, row: int) -> int:
        """The first line a block cites: its first attached comment's, else its own."""
        return self.comment_row if self.comments else row

    def run(self) -> list[Block]:
        row = 0
        while row < len(self.lines):
            stripped = self.lines[row].strip()
            if stripped.startswith("#"):
                if not self.comments:
                    self.comment_row = row
                self.comments.append(stripped[1:].strip())
                row += 1
            elif stripped.startswith("["):
                self._header(row, stripped)
                row += 1
            elif stripped:
                row = self._key(row)
            else:
                self._loose_comments(row)
                row += 1
        self._loose_comments(len(self.lines))
        return self.blocks

    def _unread(self, row: int) -> None:
        """A line tomllib accepted that the scanner cannot place: kept as text, and
        named in `unplaced` so coverage shows the scanner missed it."""
        self._loose_comments(row)
        self.blocks.append(
            Block(
                id=f"b{len(self.blocks)}",
                type="paragraph",
                text=self.lines[row].strip(),
                locator=Locator(line_start=row + 1, line_end=row + 1),
                section_path=tuple(s.name for s in self.table),
                parent=self.table_id,
            )
        )
        self.unplaced.append(row + 1)

    def _loose_comments(self, end: int) -> None:
        """A run of comments that no table or key follows (a gap or the end of the
        file comes first) is a paragraph of its own, so its words stay searchable."""
        if not self.comments:
            return
        self.blocks.append(
            Block(
                id=f"b{len(self.blocks)}",
                type="paragraph",
                text="\n".join(f"# {c}" for c in self.comments),
                locator=Locator(line_start=self.comment_row + 1, line_end=end),
                section_path=tuple(s.name for s in self.table),
                parent=self.table_id,
                attrs={"comments": list(self.comments)},
            )
        )
        self.comments = []

    def _header_steps(self, parts: list[str], is_aot: bool) -> list[_Step]:
        """The header's path, numbering every array of tables it passes through."""
        steps: list[_Step] = []
        ancestors: list[int] = []
        prefix = 0  # the document root
        for k, name in enumerate(parts):
            prefix = self.path_ids.setdefault((prefix, name), len(self.path_ids) + 1)
            if is_aot and k == len(parts) - 1:
                if prefix not in self.aot_counts:
                    for ancestor in ancestors:
                        self.aot_under.setdefault(ancestor, set()).add(prefix)
                self.aot_counts[prefix] = self.aot_counts.get(prefix, -1) + 1
                # A new element restarts the numbering of tables under it.
                for nested in self.aot_under.pop(prefix, set()):
                    self.aot_counts.pop(nested, None)
            ancestors.append(prefix)
            steps.append(_Step(name, self.aot_counts.get(prefix, -1)))
        return steps

    def _header(self, row: int, stripped: str) -> None:
        is_aot = stripped.startswith("[[")
        parsed = _read_key(stripped, 2 if is_aot else 1)
        if parsed is None:
            self._unread(row)
            return
        parts, after = parsed
        rest = stripped[after:]
        comment = rest.split("#", 1)[1].strip() if "#" in rest else ""
        self.table = self._header_steps(parts, is_aot)
        self.table_id = f"b{len(self.blocks)}"
        path = _path_text(self.table)
        attrs: dict[str, Any] = {"array_of_tables": True} if is_aot else {}
        if self.comments or comment:
            attrs["comments"] = [*self.comments, *([comment] if comment else [])]
        self.blocks.append(
            Block(
                id=self.table_id,
                type="toml_table",
                text="\n".join([*(f"# {c}" for c in self.comments), stripped]),
                locator=Locator(
                    line_start=self._first_row(row) + 1, line_end=row + 1, key_path=path
                ),
                section_path=tuple(s.name for s in self.table),
                keys=tuple(dict.fromkeys([path, _path_text(self.table, indexed=False)])),
                attrs=attrs,
            )
        )
        self.comments = []

    def _key(self, row: int) -> int:
        """Record the key on `row`; returns the row after its value."""
        line = self.lines[row]
        parsed = _read_key(line, 0)
        if parsed is None or parsed[1] >= len(line) or line[parsed[1]] != "=":
            self._unread(row)
            return row + 1
        parts, eq = parsed
        last, raw, comment = _value_end(self.lines, row, eq + 1)
        steps = [*self.table, *(_Step(p) for p in parts)]
        path = _path_text(steps)
        try:
            value = _lookup(self.doc, steps)
        except (KeyError, IndexError, TypeError):
            value = None
        comments = [*self.comments, *([comment] if comment else [])]
        attrs: dict[str, Any] = {"value_type": _type_of(value) if value is not None else ""}
        if not _deeper_than(value, _VALUE_DEPTH):  # a deeper one is in the block's text alone
            attrs["value"] = _jsonable(value)
        if comments:
            attrs["comments"] = comments
        rendered = f"{path} = {raw}" + (f"  # {comment}" if comment else "")
        self.blocks.append(
            Block(
                id=f"b{len(self.blocks)}",
                type="toml_key",
                text="\n".join([*(f"# {c}" for c in self.comments), rendered]),
                locator=Locator(
                    line_start=self._first_row(row) + 1, line_end=last + 1, key_path=path
                ),
                section_path=tuple(s.name for s in self.table),
                parent=self.table_id,
                keys=tuple(dict.fromkeys([path, _path_text(steps, indexed=False)])),
                attrs=attrs,
            )
        )
        self.comments = []
        return last + 1


def _wide_integer(doc: dict[str, Any]) -> str:
    """The key path of an integer outside signed 64 bits, which TOML makes an
    error and `tomllib` reads anyway, or ''. Walked without recursion in key
    order (a table's keys together, as `tomllib` holds them: a table reopened
    later in the file comes with its first part); each entry holds its parent,
    so a path is spelled only for the integer it names."""
    stack: list[tuple[Any, Any]] = [(None, doc)]  # ((parent entry, key or index), value)
    while stack:
        entry, v = stack.pop()
        if isinstance(v, dict):
            stack.extend(((entry, k), x) for k, x in reversed(v.items()))
        elif isinstance(v, list):
            stack.extend(((entry, i), x) for i, x in reversed(list(enumerate(v))))
        elif isinstance(v, int) and not isinstance(v, bool) and not -(2**63) <= v < 2**63:
            return _entry_path(entry)
    return ""


def _entry_path(entry: Any) -> str:
    parts: list[str] = []
    while entry is not None:
        entry, step = entry
        parts.append(f"[{step}]" if isinstance(step, int) else _path_text([_Step(step)]))
    path = ""
    for part in reversed(parts):
        path += part if part.startswith("[") or not path else f".{part}"
    return path


def _decoded(text: str) -> dict[str, Any]:
    """`tomllib.loads`, refusing what TOML refuses and `tomllib` does not, each
    reason naming where it is when it can."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise
    except ValueError as e:  # `int()` refusing a decimal past Python's digit limit
        limit = sys.get_int_max_str_digits()
        raise ValueError(f"an integer of over {limit:,} digits") from e
    path = _wide_integer(doc)
    if path:
        raise ValueError(f"{path} is an integer outside signed 64 bits")
    return doc


def extract_toml(text: str) -> Artifact:
    try:
        doc = _decoded(text)
    except (ValueError, RecursionError) as e:  # tomllib recurses per nesting level
        fallback, _ = paragraph_blocks(text)
        return Artifact(
            format="toml",
            parser="loom-toml",
            parser_version=on_python(PARSER_VERSION),  # `tomllib` decides validity
            blocks=tuple(fallback),
            coverage=Coverage(
                "keys",
                0,
                0,
                failed=(Failure("file", f"invalid TOML: {e}"),),
                lost=("toml_types",),
            ),
        )
    scanner = _Scanner(doc, text.split("\n"))
    blocks = scanner.run()
    keys = [b for b in blocks if b.type == "toml_key"]
    unresolved = [b for b in keys if not b.attrs["value_type"]]  # no value at its path
    failed = [Failure(b.locator.key_path, "no value at this path") for b in unresolved]
    failed += [Failure(f"line {n}", "the scanner could not read it") for n in scanner.unplaced]
    return Artifact(
        format="toml",
        parser="loom-toml",
        parser_version=on_python(PARSER_VERSION),  # `tomllib` decides validity
        blocks=tuple(blocks),
        coverage=Coverage(
            unit="keys",
            total=len(keys),
            extracted=len(keys) - len(unresolved),
            failed=tuple(failed),
        ),
    )
