"""Source bytes into the normalized extraction artifact.

`detect_format` decides what a file is from its content first and its name
second: a name that claims a format its bytes do not have is refused, never
read as something else. `extract` decodes text strictly (UTF-8, UTF-16 or
UTF-32 by its byte-order mark, or a declared encoding; no lossy replacement of
undecodable bytes) and runs the format's parser. Every failure is an
`ExtractionError` with a typed `code`, so an unsupported, corrupt or
mislabelled file is reported as such rather than ingested as binary noise.

Pure and I/O-free.
"""

from __future__ import annotations

import codecs
import io
import zipfile
from dataclasses import dataclass, replace

from loom_extractor.artifact import Artifact, within_contract
from loom_extractor.sql import UnknownDialect, extract_sql
from loom_extractor.text import extract_text
from loom_extractor.tomlfile import extract_toml

# Every format a source can be, and whether this build parses it.
PARSED_FORMATS = ("markdown", "html", "text", "sql", "toml")
KNOWN_FORMATS = (*PARSED_FORMATS, "pdf", "docx", "pptx", "xlsx", "csv", "parquet")

_EXTENSIONS = {
    "md": "markdown",
    "markdown": "markdown",
    "mdx": "markdown",
    "html": "html",
    "htm": "html",
    "xhtml": "html",
    "sql": "sql",
    "toml": "toml",
    "txt": "text",
    "text": "text",
    "rst": "text",
    "log": "text",
    "pdf": "pdf",
    "docx": "docx",
    "pptx": "pptx",
    "xlsx": "xlsx",
    "csv": "csv",
    "tsv": "csv",
    "parquet": "parquet",
}
# Legacy Office formats, refused by name; their content is recognised too, so
# they are never read as text.
_LEGACY = {"doc", "xls", "ppt"}
_BINARY = ("pdf", "docx", "pptx", "xlsx", "parquet")


class ExtractionError(Exception):
    """A source that cannot become an artifact. `code` is one of
    `unsupported_format`, `format_mismatch`, `format_unavailable`,
    `encoding`, `corrupt`, `invalid_option`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Options:
    """Caller overrides for what a file cannot say about itself."""

    format: str = ""  # one of KNOWN_FORMATS; overrides the name, never the content
    # A Python codec name; unset, a byte-order mark picks UTF-16 or UTF-32, else UTF-8.
    encoding: str = ""
    sql_dialect: str = ""  # SQL only: another format ignores it and does not record it


def _zip_format(data: bytes) -> str:
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except (zipfile.BadZipFile, ValueError, OSError) as e:  # a bad entry name or header too
        raise ExtractionError("corrupt", f"not a readable zip container: {e}") from e
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    raise ExtractionError(
        "unsupported_format", "a zip archive that is not a DOCX, PPTX or XLSX file"
    )


def _content_format(data: bytes) -> str:
    """The binary format the bytes are, or '' for text."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return _zip_format(data)
    if data.startswith(b"PAR1") and data.endswith(b"PAR1"):
        return "parquet"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise ExtractionError(
            "unsupported_format",
            "a legacy binary Office file (DOC, XLS, PPT); save it as DOCX, XLSX or PPTX",
        )
    return ""


def detect_format(data: bytes, filename: str, declared: str = "") -> str:
    if declared and declared not in KNOWN_FORMATS:
        raise ExtractionError("invalid_option", f"unknown format {declared!r}")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _LEGACY and not declared:  # a declared format overrides the name
        raise ExtractionError(
            "unsupported_format", f".{ext} files are not supported; save it as {ext}x"
        )
    by_content = _content_format(data)
    by_name = declared or _EXTENSIONS.get(ext, "")
    if by_content:
        if by_name and by_name != by_content:
            raise ExtractionError("format_mismatch", f"the file is {by_content}, not {by_name}")
        return by_content
    if by_name in _BINARY:
        raise ExtractionError("format_mismatch", f"the file is not a {by_name} file")
    return by_name or "text"


_BYTE_ORDER_CODECS = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


def decode(data: bytes, encoding: str = "") -> str:
    """Text from bytes, strictly. A byte-order mark picks UTF-8, UTF-16 or
    UTF-32; a NUL byte in undeclared text means binary."""
    if not encoding:
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            encoding = "utf-32"
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif b"\x00" in data:  # UTF-8 text, a byte-order mark or not, holds no NUL
            raise ExtractionError("unsupported_format", "binary content of no supported format")
        elif data.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8"
    skipped = 0  # bytes of a byte order mark left out, for the offset of an error
    try:
        codec = codecs.lookup(encoding or "utf-8").name
        if codec in ("utf-8", "utf-8-sig") and data.startswith(b"\xef\xbb\xbf"):
            data, skipped = data[3:], 3  # declared or not, the mark is not text
        # Without its mark, plain UTF-8: `utf-8-sig` would take a second one too.
        text = data.decode("utf-8" if skipped else codec)
    except LookupError as e:
        raise ExtractionError("invalid_option", f"unknown encoding {encoding!r}") from e
    except UnicodeDecodeError as e:
        raise ExtractionError(
            "encoding",
            f"not valid {(encoding or 'utf-8').upper()} at byte {e.start + skipped};"
            " declare the file's encoding",
        ) from e
    # A byte-order codec (`utf-16-le`) keeps its mark as a character; it is not text.
    # The other UTF codecs have consumed theirs, so a U+FEFF left there is the text's.
    if codec in _BYTE_ORDER_CODECS and text.startswith("\ufeff"):
        text = text[1:]
    return text


def extract(data: bytes, filename: str, options: Options | None = None) -> Artifact:
    """The artifact of one source revision's bytes."""
    opts = options or Options()
    fmt = detect_format(data, filename, opts.format)
    if fmt not in PARSED_FORMATS:
        raise ExtractionError("format_unavailable", f"{fmt} files are not parsed by this build")
    artifact = within_contract(_parse(decode(data, opts.encoding), fmt, opts))
    if opts.encoding:
        # The codec's own name, so `utf8` and `UTF-8` address one artifact.
        codec = codecs.lookup(opts.encoding).name
        artifact = replace(artifact, options={**artifact.options, "encoding": codec})
    return artifact


def _parse(text: str, fmt: str, opts: Options) -> Artifact:
    if fmt == "sql":
        try:
            return extract_sql(text, dialect=opts.sql_dialect)
        except UnknownDialect as e:
            raise ExtractionError("invalid_option", str(e)) from e
    if fmt == "toml":
        return extract_toml(text)
    return extract_text(text, fmt)
