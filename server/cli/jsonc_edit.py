"""Insert one key into a JSONC object and leave every other byte in place.

A full rewrite drops comments and trailing commas. Zed settings are JSONC, so a
merge that cannot locate one object span raises and the caller keeps the
original bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
# not a setting: nesting past this is refused so the call stack stays bounded
_MAX_DEPTH = 64
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class JsoncEditError(Exception):
    """The text has no single span that can take the key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Member:
    key: str
    value: Any
    value_start: int
    value_end: int
    comma_at: int | None


@dataclass(frozen=True)
class _Object:
    start: int
    end: int
    members: tuple[_Member, ...]


def upsert_object_key(
    text: str, section: str, name: str, entry: dict[str, Any]
) -> tuple[str, bool]:
    """Return the text with `section.name` set to `entry`, and whether it changed.

    An entry that is already equal is left byte for byte. Raises JsoncEditError
    when the root is not an object, `section` is not one object, or `name` is
    duplicated there.
    """
    root = _parse_root(text)
    section_member = _only(root, section)
    if section_member is None:
        return _insert_member(text, root, section, {name: entry}), True
    if not isinstance(section_member.value, _Object):
        raise JsoncEditError(f"{section} is not an object")
    current = _only(section_member.value, name)
    if current is None:
        return _insert_member(text, section_member.value, name, entry), True
    if _to_data(current.value) == entry:
        return text, False
    indent = _line_indent(text, current.value_start)
    rendered = _render(entry, indent)
    return text[: current.value_start] + rendered + text[current.value_end :], True


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


def _line_indent(text: str, pos: int) -> str:
    newline = text.rfind("\n", 0, pos)
    start = 0 if newline < 0 else newline + 1
    end = start
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[start:end]


def _render(value: object, base_indent: str) -> str:
    lines = json.dumps(value, indent=2).split("\n")
    if len(lines) == 1:
        return lines[0]
    return "\n".join([lines[0], *[base_indent + line for line in lines[1:]]])


def _insert_member(text: str, obj: _Object, key: str, value: object) -> str:
    brace = obj.end - 1
    newline = text.rfind("\n", obj.start, brace)
    if newline < 0:
        member_indent = "  "
        prefix = "\n"
        at = brace
    else:
        close_indent = text[newline + 1 : brace]
        if close_indent.strip() != "":
            member_indent = "  "
        elif set(close_indent) <= {"\t"} and close_indent.startswith("\t"):
            member_indent = close_indent + "\t"
        else:
            member_indent = close_indent + "  "
        prefix = ""
        at = newline + 1
    piece = f"{prefix}{member_indent}{json.dumps(key)}: {_render(value, member_indent)}\n"
    if obj.members and obj.members[-1].comma_at is None:
        value_end = obj.members[-1].value_end
        if value_end > at:
            raise JsoncEditError("could not place the new key")
        text = text[:value_end] + "," + text[value_end:]
        at += 1
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
        self.i += 1
        chars: list[str] = []
        while self.i < len(text):
            char = text[self.i]
            if char == '"':
                self.i += 1
                return "".join(chars)
            if char == "\\":
                self.i += 1
                if self.i >= len(text):
                    raise JsoncEditError("unterminated string")
                esc = text[self.i]
                self.i += 1
                if esc in _ESCAPES:
                    chars.append(_ESCAPES[esc])
                    continue
                if esc == "u":
                    hex_digits = text[self.i : self.i + 4]
                    if len(hex_digits) < 4 or any(
                        c not in "0123456789abcdefABCDEF" for c in hex_digits
                    ):
                        raise JsoncEditError("bad unicode escape")
                    chars.append(chr(int(hex_digits, 16)))
                    self.i += 4
                    continue
                raise JsoncEditError("bad escape")
            if ord(char) < 0x20:
                raise JsoncEditError("unterminated string")
            chars.append(char)
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
        return json.loads(match.group(0))

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
            members.append(_Member(key, value, value_start, value_end, comma_at))

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
