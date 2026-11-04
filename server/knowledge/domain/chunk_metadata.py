"""What a `loom.chunks` row's `chunk_metadata` records about the chunk, read in one
place: the section path the outline reads, the document revision and format the
chunk was written as, and for a chunk of an extracted file its blocks' type and
where it sits in the source (the locator a citation opens). Pure.

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
    # The document revision and format this chunk was written as; 0 and '' for a chunk
    # written before they were recorded ('' is also the format of text a caller extracted).
    revision: int = 0
    format: str = ""
    # For an extracted file's chunk: its blocks' type and its place in the source.
    block_type: str = ""
    locator: dict[str, Any] | None = None


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
    revision = meta.get("revision")
    return ChunkStructure(
        section_path=tuple(str(p) for p in path if str(p)) if isinstance(path, list) else (),
        revision=revision if isinstance(revision, int) and revision >= 0 else 0,
        format=str(meta.get("format") or ""),
        block_type=str(meta.get("block_type") or ""),
        locator=locator if isinstance(locator, dict) else None,
    )
