"""The parts of an Office Open XML package (DOCX, PPTX, XLSX) read with the
standard library: a zip of XML parts linked by relationship parts. A part is
stored or deflated (as OOXML writes them; another method is refused). A part
read whole (`xml`) declares at most MAX_PART_BYTES uncompressed and is inflated
only up to what it declares, so a zip bomb stops at that bound, and its
checksum is checked over that. A streamed part (`stream`) is not refused for
its declared size: it is read up to what the package inflates to in all
(MAX_PACKAGE_BYTES, parts read whole included), MAX_ELEMENTS in all, a tag of
at most MAX_TOKEN_BYTES, MAX_DEPTH deep and MAX_NAMES distinct names of at most
MAX_NAME_CHARS, and cut there. A part with a DOCTYPE is refused: OOXML declares
none, and without one no entity is expanded. A part that cannot be read is a
PartError, which fails the unit it belongs to.

Pure and I/O-free.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
import zlib
from typing import Any, Protocol
from xml.etree import ElementTree as ET
from xml.parsers import expat

# The most one part may hold uncompressed, the most all of a package's parts may
# (what is read of them in all), and how much of a part a stream inflates at a time.
MAX_PART_BYTES = 256 * 1024 * 1024  # not a setting: a bound on hostile input
MAX_PACKAGE_BYTES = 512 * 1024 * 1024  # not a setting: the same bound, for many parts
# The most elements the streamed parts may hold in all: what bounds the time a
# package is read for (a part of 512 MiB of `<c/>` is 134 M of them).
MAX_ELEMENTS = 25_000_000  # not a setting: a bound on hostile input
# The most a streamed part may hold between two events expat reports (one tag
# with its attributes): what one token takes in memory.
MAX_TOKEN_BYTES = 16 * 1024 * 1024  # not a setting: a bound on hostile input
# The deepest a streamed part may nest (a sheet nests five deep): what expat's
# stack of open elements takes in memory.
MAX_DEPTH = 64  # not a setting: a bound on hostile input
# The most distinct element and attribute names a streamed part may use (a sheet
# uses a few dozen): the parser keeps each one for as long as it reads.
MAX_NAMES = 4096  # not a setting: a bound on hostile input
MAX_NAME_CHARS = 1024  # not a setting: an element or attribute name (a sheet's are short)
_CHUNK = 1024 * 1024
_TEXT_BUFFER = 64 * 1024
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


class PartError(Exception):
    """A part that cannot be read: missing, over the bound, damaged, or not XML."""


class Cut(PartError):
    """A streamed part read no further: what a handler took before it stands, and
    the parts after it are still read (a tag too large, nesting too deep, too
    many names or a name too long; a handler raises it too, to stop reading)."""


class Exhausted(Cut):
    """The streamed parts went past what a package is read for (MAX_ELEMENTS,
    MAX_PACKAGE_BYTES): no part after it is read."""


_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
# What reading a damaged entry raises: a bad checksum or header (a name that is
# not UTF-8 among them), a truncated deflate stream, an encrypted entry.
_DAMAGED = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    RuntimeError,
    NotImplementedError,
    ValueError,
)


# What parsing a part that is not XML raises: an encoding expat cannot read too.
_NOT_XML = (expat.ExpatError, ValueError, LookupError)


class Package:
    def __init__(self, data: bytes) -> None:
        self._zip = zipfile.ZipFile(io.BytesIO(data))
        self._names = set(self._zip.namelist())
        self._read = 0  # bytes inflated so far, from every part but media
        self._media = 0  # bytes inflated so far, from media parts (`media`)
        self._elements = 0  # elements streamed so far, from every part

    def has(self, name: str) -> bool:
        return name in self._names

    def _entry(self, name: str) -> zipfile.ZipInfo:
        if name not in self._names:
            raise PartError(f"{name} is missing")
        info = self._zip.getinfo(name)
        if info.compress_type not in _METHODS:
            raise PartError(f"{name} is compressed with method {info.compress_type}")
        return info

    def _info(self, name: str, limit: int | None = None) -> zipfile.ZipInfo:
        """A part read whole: at most `limit` bytes (MAX_PART_BYTES), and within
        what the package may inflate to."""
        limit = MAX_PART_BYTES if limit is None else limit
        info = self._entry(name)
        if info.file_size > limit:
            raise PartError(f"{name} is over {limit} bytes")
        if self._read + info.file_size > MAX_PACKAGE_BYTES:
            raise PartError(f"{name} would inflate the package past {MAX_PACKAGE_BYTES} bytes")
        self._read += info.file_size
        return info

    def xml(self, name: str, limit: int | None = None) -> ET.Element:
        """Part `name` as a tree, when it is at most `limit` bytes (MAX_PART_BYTES)."""
        info = self._info(name, limit)
        try:
            with self._zip.open(info) as f:
                # One read of the declared size: inflating stops there, and the
                # checksum is checked once the declared bytes are read.
                raw = f.read(info.file_size)
        except _DAMAGED as e:
            raise PartError(f"{name} is damaged: {type(e).__name__}") from e
        return _parse(name, raw)

    def media(self, name: str, limit: int) -> bytes:
        """Media part `name`'s bytes (a picture), when it is at most `limit`
        bytes. Media inflate within a bound of their own, so pictures read never
        cost the package's text parts their room."""
        info = self._entry(name)
        if info.file_size > limit:
            raise PartError(f"{name} is over {limit} bytes")
        if self._media + info.file_size > MAX_PACKAGE_BYTES:
            raise PartError(
                f"{name} would inflate the package's media past {MAX_PACKAGE_BYTES} bytes"
            )
        self._media += info.file_size
        try:
            with self._zip.open(info) as f:
                return f.read(info.file_size)
        except _DAMAGED as e:
            raise PartError(f"{name} is damaged: {type(e).__name__}") from e

    def stream(self, name: str, handler: Handler) -> None:
        """Part `name` read through `handler`'s callbacks as it inflates, a
        chunk at a time, so a large part (a sheet's cells) is never a tree in
        memory, with the DOCTYPE refusal `xml` has. Its size is no refusal: what
        the package inflates to and its elements are counted as it is read, and
        past either it raises `Exhausted`; a tag too large, nesting too deep, too
        many names or a name too long raise `Cut`. Either way what the handler
        took stands."""
        info = self._entry(name)
        parser = _expat(name)

        depth = names = 0
        # The handlers hold the names, not the parser: a parser and a handler that
        # held each other would keep every part's handler (a sheet's merges, its
        # cells) alive until a full collection.
        interned = parser.intern

        def start(tag: str, attrs: dict[str, str]) -> None:
            nonlocal depth, names
            self._elements += 1
            depth += 1
            if self._elements > MAX_ELEMENTS:
                raise Exhausted(f"{name}: past the {MAX_ELEMENTS} elements a package is read for")
            if depth > MAX_DEPTH:
                raise Cut(f"{name}: nested deeper than {MAX_DEPTH}")
            if len(interned) > names:  # a new name, kept as long as the part is read
                names = len(interned)
                if names > MAX_NAMES:
                    raise Cut(f"{name}: more than {MAX_NAMES} element and attribute names")
                if max([len(tag), *map(len, attrs)]) > MAX_NAME_CHARS:
                    raise Cut(f"{name}: a name over {MAX_NAME_CHARS} characters")
            handler.start(_ns(tag), attrs)

        def end(tag: str) -> None:
            nonlocal depth
            depth -= 1
            handler.end(_ns(tag))

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = handler.data

        def feed(data: bytes, final: bool) -> None:
            try:
                parser.Parse(data, final)
            except _NOT_XML as e:
                raise PartError(f"{name} is not XML: {e}") from e

        left, fed = info.file_size, 0
        try:
            f = self._zip.open(info)
        except _DAMAGED as e:
            raise PartError(f"{name} is damaged: {type(e).__name__}") from e
        with f:
            while left > 0:
                try:
                    chunk = f.read(min(_CHUNK, left))
                except _DAMAGED as e:
                    raise PartError(f"{name} is damaged: {type(e).__name__}") from e
                if not chunk:
                    break
                left -= len(chunk)
                room = max(0, MAX_PACKAGE_BYTES - self._read)
                if len(chunk) > room:
                    self._read = MAX_PACKAGE_BYTES
                    feed(chunk[:room], False)
                    raise Exhausted(
                        f"{name}: past the {MAX_PACKAGE_BYTES} bytes a package inflates to"
                    )
                self._read += len(chunk)
                feed(chunk, False)
                fed += len(chunk)
                # What expat holds past its last event is one token being read.
                if fed - parser.CurrentByteIndex > MAX_TOKEN_BYTES:
                    raise Cut(f"{name}: a tag over {MAX_TOKEN_BYTES} bytes")
            feed(b"", True)

    def rels(self, name: str, limit: int | None = None) -> dict[str, tuple[str, str]]:
        """Part `name`'s relationships: `{id: (type's last segment, target part)}`,
        external targets left out; {} when it has none. Its part is a tree of at
        most `limit` bytes."""
        folder, base = posixpath.split(name)
        rels_name = posixpath.join(folder, "_rels", base + ".rels")
        if not self.has(rels_name):
            return {}
        out = {}
        for rel in self.xml(rels_name, limit).iter(_REL):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            path = target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
            kind = rel.get("Type", "").rsplit("/", 1)[-1]
            out[rel.get("Id", "")] = (kind, posixpath.normpath(path))
        return out


class Handler(Protocol):
    """What `Package.stream` calls: an element's start (its tag as ElementTree
    spells it, its attributes as expat gives them), its end, and its text."""

    def start(self, tag: str, attrs: dict[str, str]) -> None: ...
    def end(self, tag: str) -> None: ...
    def data(self, text: str) -> None: ...


def _ns(tag: str) -> str:
    return "{" + tag if "}" in tag else tag


def _expat(name: str) -> Any:
    """An expat parser that refuses a DOCTYPE in any encoding: expat reports one
    before it reads an entity the DOCTYPE declares."""
    parser = expat.ParserCreate(namespace_separator="}")
    # Text in one callback per run, not one per line or entity: what bounds the
    # callbacks a part makes is then its elements (MAX_ELEMENTS).
    parser.buffer_text = True
    parser.buffer_size = _TEXT_BUFFER

    def doctype(*_: object) -> None:
        raise PartError(f"{name} declares a DOCTYPE")

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = doctype
    return parser


def _parse(name: str, raw: bytes) -> ET.Element:
    """`raw` as an element tree."""
    builder = ET.TreeBuilder()
    parser = _expat(name)
    parser.StartElementHandler = lambda tag, attrs: builder.start(
        _ns(tag), {_ns(k): v for k, v in attrs.items()}
    )
    parser.EndElementHandler = lambda tag: builder.end(_ns(tag))
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(raw, True)
    except _NOT_XML as e:
        raise PartError(f"{name} is not XML: {e}") from e
    return builder.close()


def q(tag: str) -> str:
    """`w:p` as ElementTree spells it."""
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"
