"""Insert one key into a JSONC object.

A full rewrite drops comments and trailing commas. The edit splices after the
previous member's comma, or before the closing brace when the object has no
members, and adds a comma when the previous member had none. Those offsets
come from the parse. A newline is not a splice point: a block comment can
contain one. Every other byte stays. A merge that cannot locate one object
span raises, and the caller keeps the original bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
# not a setting: nesting past this is refused so the call stack stays bounded
_MAX_DEPTH = 64
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_HEX = "0123456789abcdefABCDEF"


def _decode_json_string(raw: str) -> str:
    """One JSON string, decoded by the stdlib.

    A surrogate pair is one character, so the key `json.dumps` writes matches
    the name and a second spelling of that character is a duplicate. An
    unpaired surrogate is not a character the UTF-8 file can hold.
    """
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JsoncEditError("bad unicode escape") from exc
    if not isinstance(value, str):
        raise JsoncEditError("bad unicode escape")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JsoncEditError("unpaired surrogate") from exc
    return value


class JsoncEditError(Exception):
    """The text has no single span that can take the key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Member:
    key: str
    value: Any
    key_start: int
    value_start: int
    value_end: int
    comma_at: int | None


@dataclass(frozen=True)
class _Object:
    start: int
    end: int
    members: tuple[_Member, ...]


def upsert_nested_key(
    text: str, sections: tuple[str, ...], name: str, entry: dict[str, Any]
) -> tuple[str, bool]:
    """Return the text with `entry` set at `sections` + `name`, and whether it changed.

    With sections ("mcp", "servers"), `mcp.servers.name` is set. The first section
    missing on the way down is inserted with the rest of the path inside it. An entry
    that is already equal is left byte for byte. Raises JsoncEditError when the root
    is not an object, a section on the path is not one object, or `name` is
    duplicated in the last one.
    """
    if not sections:
        raise ValueError("sections must name at least one object")
    path = (*sections, name)
    newline = _fragment_newline(text)
    obj = _parse_root(text)
    for depth, key in enumerate(path[:-1]):
        member = _only(obj, key)
        if member is None:
            value: object = entry
            for inner in reversed(path[depth + 1 :]):
                value = {inner: value}
            return _commit(text, obj, key, value, path, entry, newline)
        if not isinstance(member.value, _Object):
            raise JsoncEditError(f"{'.'.join(path[: depth + 1])} is not an object")
        obj = member.value
    current = _only(obj, name)
    if current is None:
        return _commit(text, obj, name, entry, path, entry, newline)
    if _to_data(current.value) == entry:
        return text, False
    indent = _indent_before(text, current.key_start) or "  "
    rendered = _render(entry, indent, newline)
    updated = text[: current.value_start] + rendered + text[current.value_end :]
    _require_entry(updated, path, entry)
    return updated, True


def _only(obj: _Object, key: str) -> _Member | None:
    found = [member for member in obj.members if member.key == key]
    if len(found) > 1:
        raise JsoncEditError(f"duplicate key {json.dumps(key)}")
    return found[0] if found else None


def _to_data(value: Any) -> Any:
    if isinstance(value, _Object):
        return {member.key: _to_data(member.value) for member in value.members}
    if isinstance(value, list):
        return [_to_data(item) for item in value]
    return value


def _indent_before(text: str, pos: int) -> str:
    """Spaces and tabs that touch pos. A newline stops the scan, so a comment
    on an earlier line cannot supply the indent."""
    start = pos
    while start > 0 and text[start - 1] in " \t":
        start -= 1
    return text[start:pos]


def _fragment_newline(text: str) -> str:
    """The ending the new fragment uses.

    A file whose breaks are all CR LF gets CR LF. A file whose breaks are
    all LF, or that has none, gets LF. A mix is not rewritten; the fragment
    uses LF.
    """
    crlf = text.count("\r\n")
    bare_lf = text.count("\n") - crlf
    bare_cr = text.count("\r") - crlf
    if crlf and bare_lf == 0 and bare_cr == 0:
        return "\r\n"
    return "\n"


def _commit(
    text: str,
    obj: _Object,
    key: str,
    value: object,
    path: tuple[str, ...],
    entry: dict[str, Any],
    newline: str,
) -> tuple[str, bool]:
    updated = _insert_member(text, obj, key, value, newline)
    _require_entry(updated, path, entry)
    return updated, True


def _require_entry(text: str, path: tuple[str, ...], entry: dict[str, Any]) -> None:
    """The written text has to parse back to the entry. A splice inside a
    comment is syntactically quiet and must not count as success."""
    value: Any = _parse_root(text)
    for key in path:
        member = _only(value, key) if isinstance(value, _Object) else None
        if member is None:
            raise JsoncEditError("could not place the new key")
        value = member.value
    if _to_data(value) != entry:
        raise JsoncEditError("could not place the new key")


def _render(value: object, base_indent: str, newline: str) -> str:
    lines = json.dumps(value, indent=2).split("\n")
    if len(lines) == 1:
        return lines[0]
    return newline.join([lines[0], *[base_indent + line for line in lines[1:]]])


def _insert_member(text: str, obj: _Object, key: str, value: object, newline: str) -> str:
    # Token offsets from the parse. The nearest newline can sit inside a
    # block comment, so it is not a place to splice.
    if obj.members:
        last = obj.members[-1]
        if last.comma_at is None:
            text = text[: last.value_end] + "," + text[last.value_end :]
            at = last.value_end + 1
        else:
            at = last.comma_at + 1
        member_indent = _indent_before(text, last.key_start) or "  "
    else:
        at = obj.end - 1
        member_indent = _indent_before(text, obj.start) + "  "
    rendered = _render(value, member_indent, newline)
    piece = f"{newline}{member_indent}{json.dumps(key)}: {rendered}{newline}"
    return text[:at] + piece + text[at:]


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.depth = 0

    def _skip(self) -> None:
        text = self.text
        n = len(text)
        while self.i < n:
            char = text[self.i]
            if char in " \t\r\n":
                self.i += 1
                continue
            if char == "/" and self.i + 1 < n and text[self.i + 1] in "/*":
                if text[self.i + 1] == "/":
                    self.i += 2
                    newline = text.find("\n", self.i)
                    self.i = n if newline < 0 else newline
                    continue
                end = text.find("*/", self.i + 2)
                if end < 0:
                    raise JsoncEditError("unterminated comment")
                self.i = end + 2
                continue
            return

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise JsoncEditError("nesting is too deep")

    def _parse_string(self) -> str:
        text = self.text
        start = self.i
        if start >= len(text) or text[start] != '"':
            raise JsoncEditError("unterminated string")
        self.i += 1
        while self.i < len(text):
            char = text[self.i]
            if char == '"':
                self.i += 1
                return _decode_json_string(text[start : self.i])
            if char == "\\":
                self.i += 1
                if self.i >= len(text):
                    raise JsoncEditError("unterminated string")
                esc = text[self.i]
                self.i += 1
                if esc in _ESCAPES:
                    continue
                if esc == "u":
                    hex_digits = text[self.i : self.i + 4]
                    if len(hex_digits) < 4 or any(c not in _HEX for c in hex_digits):
                        raise JsoncEditError("bad unicode escape")
                    self.i += 4
                    continue
                raise JsoncEditError("bad escape")
            if ord(char) < 0x20:
                raise JsoncEditError("unterminated string")
            self.i += 1
        raise JsoncEditError("unterminated string")

    def _word(self, word: str, value: Any) -> Any:
        if not self.text.startswith(word, self.i):
            raise JsoncEditError("invalid JSON")
        nxt = self.i + len(word)
        if nxt < len(self.text) and (self.text[nxt].isalnum() or self.text[nxt] == "_"):
            raise JsoncEditError("invalid JSON")
        self.i = nxt
        return value

    def _parse_number(self) -> Any:
        match = _NUMBER.match(self.text, self.i)
        if match is None:
            raise JsoncEditError("invalid number")
        self.i = match.end()
        try:
            return json.loads(match.group(0))
        except ValueError as exc:
            raise JsoncEditError("invalid number") from exc

    def _parse_value(self) -> Any:
        self._skip()
        if self.i >= len(self.text):
            raise JsoncEditError("unexpected end")
        char = self.text[self.i]
        if char == "{":
            return self._parse_object()
        if char == "[":
            return self._parse_array()
        if char == '"':
            return self._parse_string()
        if char == "t":
            return self._word("true", True)
        if char == "f":
            return self._word("false", False)
        if char == "n":
            return self._word("null", None)
        if char == "-" or char.isdigit():
            return self._parse_number()
        raise JsoncEditError("invalid JSON")

    def _parse_object(self) -> _Object:
        start = self.i
        self.i += 1
        self._enter()
        members: list[_Member] = []
        text = self.text
        while True:
            self._skip()
            if self.i >= len(text):
                raise JsoncEditError("unterminated object")
            if text[self.i] == "}":
                self.i += 1
                self.depth -= 1
                return _Object(start, self.i, tuple(members))
            if text[self.i] != '"':
                raise JsoncEditError("object key is not a string")
            key_start = self.i
            key = self._parse_string()
            self._skip()
            if self.i >= len(text) or text[self.i] != ":":
                raise JsoncEditError("missing colon")
            self.i += 1
            self._skip()
            value_start = self.i
            value = self._parse_value()
            value_end = self.i
            self._skip()
            comma_at = None
            if self.i < len(text) and text[self.i] == ",":
                comma_at = self.i
                self.i += 1
            elif self.i < len(text) and text[self.i] != "}":
                raise JsoncEditError("expected comma or end of object")
            members.append(_Member(key, value, key_start, value_start, value_end, comma_at))

    def _parse_array(self) -> list[Any]:
        self.i += 1
        self._enter()
        items: list[Any] = []
        text = self.text
        while True:
            self._skip()
            if self.i >= len(text):
                raise JsoncEditError("unterminated array")
            if text[self.i] == "]":
                self.i += 1
                self.depth -= 1
                return items
            items.append(self._parse_value())
            self._skip()
            if self.i < len(text) and text[self.i] == ",":
                self.i += 1
                self._skip()
                if self.i < len(text) and text[self.i] == "]":
                    self.i += 1
                    self.depth -= 1
                    return items
                continue
            if self.i < len(text) and text[self.i] == "]":
                self.i += 1
                self.depth -= 1
                return items
            raise JsoncEditError("expected comma or end of array")


def _parse_root(text: str) -> _Object:
    parser = _Parser(text)
    if text.startswith("\ufeff"):
        parser.i = 1
    parser._skip()
    if parser.i >= len(text) or text[parser.i] != "{":
        raise JsoncEditError("not a JSON object")
    obj = parser._parse_object()
    parser._skip()
    if parser.i != len(text):
        raise JsoncEditError("trailing data")
    if not isinstance(obj, _Object):
        raise JsoncEditError("not a JSON object")
    return obj
