"""What a `loom.chunks` row's `chunk_metadata` records about the chunk, read in one
place: the section path the outline reads and its depth (and, for text, its
section's index and where the chunk starts), the document revision and format the
chunk was written as, and for a chunk of an extracted file its blocks' type and
where it sits in the source (the locator a citation opens), and whether the section is
noise (`section_value`). Pure.

A field the row does not carry reads empty: a chunk written before it was
recorded, or metadata that does not parse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChunkStructure:
    section_path: tuple[str, ...] = ()
    section_depth: int = 0  # recorded with the section path
    # For text a caller extracted: the section's index in the document and the
    # chunk's first character in it; 0 when not recorded.
    section_idx: int = 0
    char_start: int = 0
    # The document revision and format this chunk was written as; 0 and '' for a chunk
    # written before they were recorded ('' is also the format of text a caller extracted).
    revision: int = 0
    format: str = ""
    # For an extracted file's chunk: its blocks' type and its place in the source.
    block_type: str = ""
    locator: dict[str, Any] | None = None
    # The section is noise (unanswered questions alone, or a dated meeting-notes or status
    # document's section with no decision): ranked below close hits and left out of
    # context.pack and memory.stack. False for a chunk written before it was recorded.
    noise: bool = False


def read_structure(chunk_metadata: object) -> ChunkStructure:
    meta: object = None
    if chunk_metadata and isinstance(chunk_metadata, str):
        try:
            meta = json.loads(chunk_metadata)
        except (TypeError, ValueError):
            meta = None
    if not isinstance(meta, dict):
        return ChunkStructure()
    path = meta.get("section_path")
    locator = meta.get("locator")
    return ChunkStructure(
        section_path=tuple(str(p) for p in path if str(p)) if isinstance(path, list) else (),
        section_depth=_count(meta.get("section_depth")),
        section_idx=_count(meta.get("section_idx")),
        char_start=_count(meta.get("char_start")),
        revision=_count(meta.get("revision")),
        format=str(meta.get("format") or ""),
        block_type=str(meta.get("block_type") or ""),
        locator=locator if isinstance(locator, dict) else None,
        noise=meta.get("noise") is True,
    )


def _count(value: object) -> int:
    """A non-negative integer field; 0 for anything else (a bool is not one)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
