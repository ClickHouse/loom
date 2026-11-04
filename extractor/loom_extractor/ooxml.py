"""The parts of an Office Open XML package (DOCX, PPTX) read with the standard
library: a zip of XML parts linked by relationship parts. A part is stored or
deflated (as OOXML writes them; another method is refused) and declares at
most MAX_PART_BYTES uncompressed; it is inflated only up to what it declares,
so a zip bomb stops at that bound, and its checksum is checked over that. A part
with a DOCTYPE is refused: OOXML declares none, and without one no entity is
expanded. A part that cannot be read is a PartError, which fails the unit it
belongs to.

Pure and I/O-free.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
import zlib
from xml.etree import ElementTree as ET
from xml.parsers import expat

# The most one part may hold uncompressed.
MAX_PART_BYTES = 256 * 1024 * 1024  # not a setting: a bound on hostile input
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


class PartError(Exception):
    """A part that cannot be read: missing, over the bound, damaged, or not XML."""


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


class Package:
    def __init__(self, data: bytes) -> None:
        self._zip = zipfile.ZipFile(io.BytesIO(data))
        self._names = set(self._zip.namelist())

    def has(self, name: str) -> bool:
        return name in self._names

    def xml(self, name: str) -> ET.Element:
        if name not in self._names:
            raise PartError(f"{name} is missing")
        info = self._zip.getinfo(name)
        if info.compress_type not in _METHODS:
            raise PartError(f"{name} is compressed with method {info.compress_type}")
        if info.file_size > MAX_PART_BYTES:
            raise PartError(f"{name} is over {MAX_PART_BYTES} bytes")
        try:
            with self._zip.open(info) as f:
                # One read of the declared size: inflating stops there, and the
                # checksum is checked once the declared bytes are read.
                raw = f.read(info.file_size)
        except _DAMAGED as e:
            raise PartError(f"{name} is damaged: {type(e).__name__}") from e
        return _parse(name, raw)

    def rels(self, name: str) -> dict[str, tuple[str, str]]:
        """Part `name`'s relationships: `{id: (type's last segment, target part)}`,
        external targets left out; {} when it has none."""
        folder, base = posixpath.split(name)
        rels_name = posixpath.join(folder, "_rels", base + ".rels")
        if not self.has(rels_name):
            return {}
        out = {}
        for rel in self.xml(rels_name).iter(_REL):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            path = target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
            kind = rel.get("Type", "").rsplit("/", 1)[-1]
            out[rel.get("Id", "")] = (kind, posixpath.normpath(path))
        return out


def _parse(name: str, raw: bytes) -> ET.Element:
    """`raw` as an element tree, refused at a DOCTYPE in any encoding: expat
    reports one before it reads an entity the DOCTYPE declares."""
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")

    def doctype(*_: object) -> None:
        raise PartError(f"{name} declares a DOCTYPE")

    def ns(tag: str) -> str:
        return "{" + tag if "}" in tag else tag

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = doctype
    parser.StartElementHandler = lambda tag, attrs: builder.start(
        ns(tag), {ns(k): v for k, v in attrs.items()}
    )
    parser.EndElementHandler = lambda tag: builder.end(ns(tag))
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(raw, True)
    except (expat.ExpatError, ValueError, LookupError) as e:  # an encoding expat cannot read too
        raise PartError(f"{name} is not XML: {e}") from e
    return builder.close()


def q(tag: str) -> str:
    """`w:p` as ElementTree spells it."""
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"
