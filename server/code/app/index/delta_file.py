"""A compiler-tier delta read from its file one value at a time.

A harness writes its delta as one JSON object (`scip_delta.ScipDelta`): a few header
values, the `files` array, the deleted paths. A large tree's full run writes hundreds
of MiB, and parsing that whole holds the bytes and every file's objects at once. Here
the file is read in three passes over two opens, each holding at most one value or one
apply round besides a small record per entry:

- `open_delta` counts its `{`, `[` and `,` bytes against `code_scip_delta_max_nodes`
  before anything parses it, then, on the same open, parses every value (the
  standard parser finds its end, pydantic's reads it), checks each file entry, and
  keeps the header, the deleted paths and each entry's path and size;
- `DeltaFile.rounds` reopens it and parses the `files` array again for the apply, a
  planned round of entries at a time.

So a delta over a cap, or one that does not parse, is refused before anything is
applied, and the memory a delta costs is one round's (`code_scip_delta_round_max_nodes`)
plus about 70 B per entry or deleted path, as many as the node cap allows, and their
paths' text (at most `PATH_BYTES` each), which the byte cap bounds. The caps hold as
read when the delta is opened. The apply's read checks that the file is the one the
first read checked (device, inode, size, modification and change times): its writer,
the harness's sandboxed job, has exited by then.
"""

from __future__ import annotations

import codecs
import json
import os
import re
from array import array
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pydantic_core
from pydantic import ValidationError

from server.code.app.index.scip_delta import ScipDelta, ScipFile
from server.code.app.index.untrusted_files import FileRefused, open_regular
from server.settings import settings

_CHUNK = 1024 * 1024
# Longest path an entry or a deleted path may name, in UTF-8 bytes (Linux's PATH_MAX):
# the paths are kept for the whole apply, so their text is bounded too.
PATH_BYTES = 4096
# Whitespace between JSON values, skipped at once
_WS = re.compile(r"[ \t\r\n]*")
# Most characters one value's text, and one round's, may hold. A real file entry has
# about 32 characters per counted byte, so for one this cap binds near 2,000,000
# counted bytes, before the round budget's default; the largest measured holds about
# 600,000.
# not a setting: it bounds the read buffer, not the work; the round budget is the knob.
_MAX_CHARS = 64_000_000
_COUNTED = (b"{", b"[", b",")
# A value that ends, or a decode error that falls, this close to the end of the text read
# so far may only be cut off there (a number, a literal, an escape): read on.
_TAIL = 8


def _counted(text: str, start: int, end: int) -> int:
    return text.count("{", start, end) + text.count("[", start, end) + text.count(",", start, end)


class _Values:
    """The JSON text of an open file, parsed one value at a time: the standard parser
    finds where each value ends, and pydantic's JSON parser reads it."""

    def __init__(self, path: Path, fd: int, round_nodes: int, size: int) -> None:
        self._path, self._fd = path, fd
        self._left = size  # bytes the file had when opened: never read past them
        self._round_nodes = round_nodes
        self._utf8 = codecs.getincrementaldecoder("utf-8")()
        self._decoder = json.JSONDecoder()
        self._text, self._at, self._eof = "", 0, False

    def _more(self, want: int) -> bool:
        if self._eof:
            return False
        data = os.read(self._fd, min(max(want, _CHUNK), self._left)) if self._left else b""
        self._left -= len(data)
        try:
            text = self._utf8.decode(data, final=not data)
        except UnicodeDecodeError:
            raise ValueError("delta invalid: not UTF-8") from None
        self._eof = not data
        self._text = self._text[self._at :] + text
        self._at = 0
        return True

    def peek(self) -> str:
        """The next character that is not whitespace, not consumed; '' at the end."""
        while True:
            text = self._text
            # `*` matches the empty string, so there is always a match
            at = self._at = cast("re.Match[str]", _WS.match(text, self._at)).end()
            if at < len(text):
                return text[at]
            if not self._more(_CHUNK):
                return ""

    def take(self, allowed: str) -> str:
        c = self.peek()
        if not c or c not in allowed:
            raise ValueError(f"delta invalid: expected one of {allowed!r}")
        self._at += 1
        return c

    def _refused(self, nodes: int, entry: int) -> FileRefused:
        """The refusal for the cap file entry `entry` passed: a setting, or the fixed text
        cap. It names the entry: the one file to look at."""
        if nodes > self._round_nodes:
            return FileRefused(
                f"file entry {entry}: {self._path}: one value holds more than "
                f"{self._round_nodes} `{{`/`[`/`,` bytes (code_scip_delta_round_max_nodes)"
            )
        return FileRefused(
            f"file entry {entry}: {self._path}: one value holds more than {_MAX_CHARS} "
            "characters (a fixed cap, not a setting)"
        )

    def span(self, *, entry: int | None = None) -> tuple[int, int, int]:
        """Read past the next JSON value: where it starts and ends in the text and its
        counted bytes; refused when it holds more than the round's counted bytes or
        characters. Only a file entry can be that large; any other value is invalid."""
        self.peek()
        while True:
            start, text = self._at, self._text
            try:
                # only where it ends: what it builds is not kept
                end = self._decoder.raw_decode(text, start)[1]
                cut = not self._eof and end > len(text) - _TAIL
            except json.JSONDecodeError as e:
                # a value cut off where the text read so far ends errs there, or at the
                # start of a string it has not closed; anywhere else the text is bad
                if self._eof or (
                    e.pos < len(text) - _TAIL and not e.msg.startswith("Unterminated string")
                ):
                    raise ValueError("delta invalid: not JSON") from None
                end, cut = len(text), True
            except (RecursionError, ValueError):  # too deep; an integer past its digit limit
                raise ValueError("delta invalid: not JSON") from None
            nodes = _counted(text, start, end)
            if nodes > self._round_nodes or end - start > _MAX_CHARS:
                if entry is None:
                    raise ValueError("delta invalid: a value outside `files` is over a round's cap")
                raise self._refused(nodes, entry)
            if not cut:
                self._at = end
                return start, end, nodes
            held = len(text) - start
            # double what is held, never past the cap
            self._more(min(held, _MAX_CHARS - held + 1))

    def value(self) -> Any:
        """The next JSON value, read as the whole-delta parse read it (pydantic's JSON),
        which refuses what the standard parser takes, a lone surrogate among it."""
        start, end, _ = self.span()
        try:
            return pydantic_core.from_json(self._text[start:end])
        except ValueError:
            raise ValueError("delta invalid: not JSON") from None

    def text(self, entry: int) -> tuple[str, int]:
        """The next file entry's text and its counted bytes."""
        start, end, nodes = self.span(entry=entry)
        return self._text[start:end], nodes

    def at_end(self) -> bool:
        return self.peek() == ""


def _top_level(values: _Values) -> Iterator[str]:
    """The top-level object's keys, each with the stream at its value: the caller reads
    the value (`value`) or walks the `files` array."""
    values.take("{")
    if values.peek() == "}":
        values.take("}")
        return
    while True:
        key = values.value()
        if not isinstance(key, str):
            raise ValueError("delta invalid: a key is not a string")
        values.take(":")
        yield key
        if values.take(",}") == "}":
            return


def _array(values: _Values) -> Iterator[None]:
    """Walk an array: yields once per element, with the stream at the element."""
    values.take("[")
    if values.peek() == "]":
        values.take("]")
        return
    while True:
        yield
        if values.take(",]") == "]":
            return


def _entry(text: str, index: int) -> ScipFile:
    """A file entry validated from its JSON text, as the whole-delta parse validated it
    (the standard parser's tree, built only to find its end, is already dropped)."""
    try:
        entry = ScipFile.model_validate_json(text)
    except ValidationError as e:
        raise ValueError(f"delta invalid: file entry {index}: {e.error_count()} error(s)") from None
    if len(entry.path.encode()) > PATH_BYTES:
        raise ValueError(f"delta invalid: file entry {index}: path over {PATH_BYTES} bytes")
    return entry


@dataclass(frozen=True)
class _Stamp:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int  # a writer can set the modification time back, not this


def _stamp(st: os.stat_result) -> _Stamp:
    return _Stamp(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _open(path: Path, max_bytes: int) -> tuple[int, _Stamp]:
    fd, st = open_regular(path, max_bytes)
    return fd, _stamp(st)


class DeltaFile:
    """A delta whose every value parsed and passed its caps (`open_delta`), applied a
    round at a time (`rounds`). It holds no open file: each read opens its own. Per
    entry it keeps the path (`paths`, which `gone_documents` reads) and two sizes in
    arrays, about 70 B and the path's length, and each deleted path in `deleted_files`,
    which may be replaced before the apply."""

    def __init__(
        self,
        path: Path,
        stamp: _Stamp,
        header: ScipDelta,
        parsed: _Parsed,
        *,
        round_nodes: int,
        max_bytes: int,
    ) -> None:
        self._path, self._stamp = path, stamp
        self.commit, self.features = header.commit, header.features
        self.deleted_files = parsed.deleted
        # each entry's path, counted bytes and characters, by index
        self.paths, self._nodes, self._chars = parsed.paths, parsed.nodes, parsed.chars
        self._round_nodes, self._max_bytes = round_nodes, max_bytes  # as read at open

    def _plan(self, max_files: int, keep: Callable[[str], bool]) -> list[array[int]]:
        """The rounds by entry index: at most `max_files` kept entries and the round's
        counted bytes and characters each; an entry alone may fill a round."""
        rounds: list[array[int]] = []
        batch = array("I")
        nodes = chars = 0
        for i, path in enumerate(self.paths):
            if not keep(path):
                continue
            n, c = self._nodes[i], self._chars[i]
            if batch and (
                len(batch) >= max_files or nodes + n > self._round_nodes or chars + c > _MAX_CHARS
            ):
                rounds.append(batch)
                batch, nodes, chars = array("I"), 0, 0
            batch.append(i)
            nodes, chars = nodes + n, chars + c
        if batch:
            rounds.append(batch)
        return rounds

    def rounds(self, max_files: int, keep: Callable[[str], bool]) -> Generator[list[ScipFile]]:
        """The entries whose path `keep` admits, then `deleted_files`, a planned round at
        a time: at most `max_files` files, and the round budget the delta was opened
        with. The deleted paths join the last round."""
        plan = self._plan(max_files, keep)
        gone, start = self.deleted_files, 0
        if plan:
            # the deleted paths fill the last round, as a delta held whole chunks them
            start = max_files - len(plan[-1])
            yield from self._read(plan, _deleted(gone[:start]))
        for at in range(start, len(gone), max_files):
            yield _deleted(gone[at : at + max_files])

    def _read(self, plan: list[array[int]], tail: list[ScipFile]) -> Iterator[list[ScipFile]]:
        """The planned rounds' entries, validated from a read of the file of its own,
        `tail` joining the last. A round is released before the next one is parsed;
        entries the plan leaves out are decoded and dropped."""
        fd, stamp = _open(self._path, self._max_bytes)
        try:
            if stamp != self._stamp:
                raise FileRefused(f"{self._path} changed while it was read")
            values = _Values(self._path, fd, self._round_nodes, self._stamp.size)
            rounds = iter(plan)
            current, at = next(rounds), 0
            batch: list[ScipFile] = []
            for key in _top_level(values):
                if key != "files":
                    _skip(values, key)
                    continue
                for index, _ in enumerate(_array(values)):
                    if index != current[at]:
                        values.span(entry=index)  # checked by the first read; not applied
                        continue
                    entry = _entry(values.text(index)[0], index)
                    if entry.path != self.paths[index]:  # the entry the plan admitted
                        raise FileRefused(f"{self._path} changed while it was read")
                    batch.append(entry)
                    del entry  # held by the round alone, released with it
                    at += 1
                    if at < len(current):
                        continue
                    following = next(rounds, None)
                    if following is None:
                        yield batch + tail
                        return  # the entries after the last round are not applied
                    yield batch
                    batch, current, at = [], following, 0
            # the file held fewer entries than the first read planned
            raise FileRefused(f"{self._path} changed while it was read")
        finally:
            os.close(fd)


# The top-level keys the delta model reads besides `files` and `deleted_files`: the
# model caps them small (a sha, a few feature names), and they are kept for the apply.
# Any other key is checked for its shape and size only, and neither it nor its value is
# kept.
_HEADER = frozenset({"delta_version", "commit", "base", "features"})
_READ = _HEADER | {"files", "deleted_files"}


def _deleted_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("delta invalid: a deleted path is not a string")
    if len(value.encode()) > PATH_BYTES:
        raise ValueError(f"delta invalid: a deleted path is over {PATH_BYTES} bytes")
    return value


def _deleted(paths: list[str]) -> list[ScipFile]:
    return [ScipFile(path=path, deleted=True) for path in paths]


def _skip(values: _Values, key: str) -> None:
    """Read past a top-level value: the deleted paths one at a time, as `open_delta`
    reads them, any other value whole."""
    if key == "deleted_files":
        for _ in _array(values):
            values.span()
    else:
        values.span()


def _count(path: Path, fd: int, stamp: _Stamp, max_nodes: int) -> None:
    """Refuse the file before anything parses it when it holds more `{`, `[` and `,`
    bytes than `max_nodes` (`code_scip_delta_max_nodes`), or when it is not the size it
    was opened at."""
    nodes = read = 0
    while data := os.read(fd, _CHUNK):
        read += len(data)
        if read > stamp.size:  # still being written: never read past what was opened
            raise FileRefused(f"{path} changed while it was read")
        nodes += sum(data.count(b) for b in _COUNTED)
    if read != stamp.size:
        raise FileRefused(f"{path} changed while it was read")
    if nodes > max_nodes:
        raise FileRefused(
            f"{path} holds {nodes} `{{`/`[`/`,` bytes; max {max_nodes} (code_scip_delta_max_nodes)"
        )


@dataclass
class _Parsed:
    """What the checking parse keeps: the small header values, the deleted paths, and
    each entry's path, counted bytes and characters."""

    header: dict[str, Any] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    nodes: array[int] = field(default_factory=lambda: array("I"))
    chars: array[int] = field(default_factory=lambda: array("Q"))


def _parse(values: _Values) -> _Parsed:
    """Parse every value once and check each file entry; keep only what the apply needs."""
    out = _Parsed()
    seen: set[str] = set()  # only the keys the delta model reads
    for key in _top_level(values):
        if key in _READ:
            if key in seen:
                raise ValueError(f"delta invalid: {key!r} twice")
            seen.add(key)
        if key == "deleted_files":
            # one path at a time: a long list is many small values, not one large one
            out.deleted = [_deleted_path(values.value()) for _ in _array(values)]
        elif key == "files":
            for index, _ in enumerate(_array(values)):
                text, nodes = values.text(index)
                out.paths.append(_entry(text, index).path)
                out.nodes.append(nodes)
                out.chars.append(len(text))
                del text
        elif key in _HEADER:
            out.header[key] = values.value()
        else:
            values.span()  # a key the model ignores: neither it nor its value is kept
    if not values.at_end():
        raise ValueError("delta invalid: text after the object")
    return out


def open_delta(path: Path) -> DeltaFile:
    """The delta a compiler-tier harness wrote, every value parsed once and checked:
    at most `code_scip_delta_file_max_mb` and `code_scip_delta_max_nodes` in all, no
    value over `code_scip_delta_round_max_nodes`, every file entry a valid one, one
    `files` array. Only an absent file raises FileNotFoundError; a link, even a
    dangling one, is refused. An invalid delta raises a short ValueError."""
    round_nodes = settings.code_scip_delta_round_max_nodes
    max_bytes = settings.code_scip_delta_file_max_mb * 1024 * 1024
    fd, stamp = _open(path, max_bytes)
    try:
        _count(path, fd, stamp, settings.code_scip_delta_max_nodes)
        os.lseek(fd, 0, os.SEEK_SET)
        parsed = _parse(_Values(path, fd, round_nodes, stamp.size))
        if _stamp(os.fstat(fd)) != stamp:  # the parse read what the count counted
            raise FileRefused(f"{path} changed while it was read")
    finally:
        os.close(fd)
    # the header without the entries and deleted paths, which were checked as read
    try:
        checked = ScipDelta.model_validate(parsed.header)
    except ValidationError as e:
        raise ValueError(f"delta invalid: {e.error_count()} error(s)") from None
    return DeltaFile(path, stamp, checked, parsed, round_nodes=round_nodes, max_bytes=max_bytes)
