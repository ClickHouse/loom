"""Document formats and the format a source is sent to an extractor as, from what
the caller says about it: a declared format, else the file name's extension,
else its media type, else `text`. The settings check extractor routes against
these names, so they live in `core`. The server reads no byte of the original
to decide; the extractor checks the content and refuses a name that claims
another format (`format_mismatch`).

Pure and I/O-free.
"""

from __future__ import annotations

KNOWN_FORMATS = (
    "markdown",
    "html",
    "text",
    "sql",
    "toml",
    "pdf",
    "docx",
    "pptx",
    "xlsx",
    "csv",
    "parquet",
)

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

# Legacy binary Office formats: not supported, refused by name before any call.
_LEGACY = {"doc", "xls", "ppt"}

_MEDIA_TYPES = {
    "text/markdown": "markdown",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
    "application/sql": "sql",
    "application/toml": "toml",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/csv": "csv",
    "text/tab-separated-values": "csv",
    "application/vnd.apache.parquet": "parquet",
}


def media_type_of(media_type: str) -> str:
    """`media_type` without its parameters, lowercased: `Text/HTML; charset=x`
    is `text/html`."""
    return media_type.split(";", 1)[0].strip().lower()


def route_key(key: str) -> str:
    """An extractor route's key as routing matches it: a format name as given,
    a media type without its parameters and lowercased; '' when it is neither
    a known format nor a media type that names one."""
    if key in KNOWN_FORMATS:
        return key
    media = media_type_of(key)
    return media if media in _MEDIA_TYPES else ""


def format_of_media_type(media_type: str) -> str:
    """The format a media type names, or '' for one that names none."""
    return _MEDIA_TYPES.get(media_type_of(media_type), "")


def format_of(name: str, media_type: str = "", declared: str = "") -> str:
    """The format to extract `name` as. A declared format outside KNOWN_FORMATS,
    or a legacy Office name with none declared, is a ValueError."""
    if declared:
        if declared not in KNOWN_FORMATS:
            raise ValueError(f"unknown format {declared!r}")
        return declared
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _LEGACY:
        raise ValueError(f".{ext} files are not supported; save it as {ext}x")
    return _EXTENSIONS.get(ext) or _MEDIA_TYPES.get(media_type_of(media_type), "text")
