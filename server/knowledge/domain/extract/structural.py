"""Chunks from an artifact's blocks, on the source's own structure.

Consecutive blocks under the same parent (a heading's section, a TOML table
with its header) pack into one chunk up to `size` characters; a SQL statement
is a chunk of its own, so a hit cites one statement. A block longer than
`size` is windowed (`size`, stepping by `stride`) and every window keeps the
block's section, so no window is embedded without its context. Heading text is
not repeated in a chunk: the section path travels beside it and goes into the
embedding input. A heading with no text under it and no subsection (a last
heading, a document of headings alone) is a chunk of its own, so its words
stay searchable.

Each chunk names the blocks it was cut from, the merged locator of their span
and the union of their exact keys.

Pure and I/O-free.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from server.knowledge.domain.chunking import SEPARATOR, window
from server.knowledge.domain.extract.artifact import Artifact, Block, Lines, Locator

# Block types cited one at a time: never packed with a neighbour.
_ALONE = {"sql_statement"}


@dataclass(frozen=True)
class StructuralChunk:
    idx: int
    text: str
    block_ids: tuple[str, ...]
    block_type: str  # its blocks' type, `mixed` when they differ
    section_path: tuple[str, ...]
    locator: Locator
    keys: tuple[str, ...] = ()

    @property
    def breadcrumb(self) -> str:
        return SEPARATOR.join(self.section_path)


def _merge(locs: list[Locator]) -> Locator:
    """The span covering `locs`; a field kept only when every part agrees
    (one key path) or it is a range (lines, chars)."""
    lines = [x for x in locs if x.line_start]
    chars = [x for x in locs if x.char_start >= 0]
    paths = {x.key_path for x in locs}
    return Locator(
        line_start=min((x.line_start for x in lines), default=0),
        line_end=max((x.line_end for x in lines), default=0),
        char_start=min((x.char_start for x in chars), default=-1),
        char_end=max((x.char_end for x in chars), default=-1),
        key_path=paths.pop() if len(paths) == 1 else "",
    )


def _windows(b: Block, *, size: int, stride: int) -> list[tuple[str, Locator]]:
    """`b` cut into windows; each window's locator is its own line and char span
    when the block's text is its source slice (it has char offsets and is not a
    heading, whose text is its cleaned title), the block's otherwise."""
    out: list[tuple[str, Locator]] = []
    lines = Lines(b.text)
    for start, text in window(b.text, size=size, stride=stride):
        loc = b.locator
        if loc.char_start >= 0 and loc.line_start and b.type != "heading":
            end = start + len(text)
            loc = replace(
                loc,
                line_start=loc.line_start + lines.of(start) - 1,
                line_end=loc.line_start + lines.of(max(start, end - 1)) - 1,
                char_start=loc.char_start + start,
                char_end=loc.char_start + end,
            )
        out.append((text, loc))
    return out


def _owner(b: Block) -> str:
    """What a block packs under: a TOML table packs with its own keys."""
    return b.id if b.type == "toml_table" else b.parent


def chunk_artifact(artifact: Artifact, *, size: int, stride: int) -> list[StructuralChunk]:
    """Dense, ordered chunks of `artifact`. An artifact with no text blocks is
    no chunks; the caller decides whether that is a failure. A stride past the
    size steps one window, so no text falls between windows."""
    stride = min(stride, size)
    chunks: list[StructuralChunk] = []
    group: list[Block] = []
    used = 0  # the group's text with its separators, as `size` counts it

    def emit(text: str, blocks: list[Block], loc: Locator) -> None:
        # One block's keys are shared, not copied: each window of a long block is a
        # chunk, and a copy per window would cost windows x keys.
        if len(blocks) == 1:
            keys = blocks[0].keys
        else:
            keys = tuple(dict.fromkeys(k for b in blocks for k in b.keys))
        chunks.append(
            StructuralChunk(
                idx=len(chunks),
                text=text,
                block_ids=tuple(b.id for b in blocks),
                block_type=blocks[0].type if len({b.type for b in blocks}) == 1 else "mixed",
                section_path=blocks[0].section_path,
                locator=loc,
                keys=keys,
            )
        )

    def flush() -> None:
        nonlocal used
        used = 0
        if group:
            emit(
                "\n\n".join(b.text for b in group), list(group), _merge([b.locator for b in group])
            )
            group.clear()

    # A heading whose section has no text of its own (a last heading, a heading
    # only document) is its own chunk, so its words stay searchable.
    bare: Block | None = None
    for b in artifact.blocks:
        if b.type == "heading":
            flush()
            if bare is not None and b.depth <= bare.depth:
                emit(bare.text, [bare], bare.locator)
            bare = b
            if len(b.text) > size:  # a paragraph over `---` can be a long heading
                for text, loc in _windows(b, size=size, stride=stride):
                    emit(text, [b], loc)
                bare = None
            continue
        if not b.text.strip():
            continue
        bare = None
        if len(b.text) > size:
            flush()
            for text, loc in _windows(b, size=size, stride=stride):
                emit(text, [b], loc)
            continue
        if b.type in _ALONE:
            flush()
            emit(b.text, [b], b.locator)
            continue
        if group and (_owner(group[0]) != _owner(b) or used + len(b.text) > size):
            flush()
        group.append(b)
        used += len(b.text) + 2
    flush()
    if bare is not None:
        emit(bare.text, [bare], bare.locator)
    return chunks
