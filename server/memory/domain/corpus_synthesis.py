"""Long-range understanding is a pass over the whole document, not a search.

Long-range understanding — summarize a 170k-token book, answer a question whose evidence
is spread over the whole text — is where retrieval loses to a full read, because
retrieval returns a slice and cannot integrate the rest. Loom's knowledge plane keeps
every chunk of every document in order with its section path. This module is the pure
part of a map-reduce over that order: chunks are grouped into windows that respect
section boundaries and a size budget, each window is read once ("map": notes with chunk
citations), the notes are folded into the answer ("reduce"), hierarchically when they do
not fit in one call.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CorpusChunk:
    source_uri: str
    idx: int
    text: str
    section_path: tuple[str, ...] = ()


@dataclass
class Window:
    chunks: list[CorpusChunk] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(c.text) for c in self.chunks)

    def cite(self) -> str:
        if not self.chunks:
            return ""
        uris = {c.source_uri for c in self.chunks}
        if len(uris) == 1:
            first, last = self.chunks[0].idx, self.chunks[-1].idx
            return f"{self.chunks[0].source_uri}#{first}-{last}"
        return ",".join(f"{c.source_uri}#{c.idx}" for c in self.chunks)


def windows(chunks: Sequence[CorpusChunk], *, window_chars: int) -> list[Window]:
    """Group in-order chunks into windows of at most `window_chars`,
    closing a window early when the section changes and the window is at
    least a third full, so a section is read together where possible. A
    single chunk larger than the budget becomes its own window."""
    out: list[Window] = []
    cur = Window()
    last_section: tuple[str, ...] | None = None
    for c in chunks:
        section_break = (
            last_section is not None
            and c.section_path != last_section
            and cur.chunks
            and cur.chars >= window_chars // 3
        )
        if cur.chunks and (cur.chars + len(c.text) > window_chars or section_break):
            out.append(cur)
            cur = Window()
        cur.chunks.append(c)
        last_section = c.section_path
    if cur.chunks:
        out.append(cur)
    return out


def fold(notes: Sequence[str], *, limit_chars: int) -> list[list[str]]:
    """Group notes into batches that fit `limit_chars` for a hierarchical
    reduce. One batch when everything fits."""
    batches: list[list[str]] = []
    cur: list[str] = []
    used = 0
    for n in notes:
        if cur and used + len(n) > limit_chars:
            batches.append(cur)
            cur, used = [], 0
        cur.append(n)
        used += len(n)
    if cur:
        batches.append(cur)
    return batches


MAP_SYSTEM = (
    "You read one part of a longer document and write notes for a later pass "
    "that will not see the text. Keep every fact, name, number, date and event "
    "that could matter for the task; keep the order of events; cite the part "
    "with the tag given. Write plain sentences, no preamble."
)


def map_prompt(task: str, window: Window) -> str:
    body = "\n\n".join(f"[{c.source_uri}#{c.idx}] {c.text}" for c in window.chunks)
    return (
        f"TASK the final pass must complete: {task}\n\n"
        f"PART {window.cite()}:\n{body}\n\n"
        "Notes for the final pass (cite parts as [source#idx]):"
    )


REDUCE_SYSTEM = (
    "You complete a task over a long document from notes written by earlier "
    "passes, each citing the part it came from. Use only the notes. Keep the "
    "citations that support what you write. If the notes do not settle a point, "
    "say so rather than guess."
)


def reduce_prompt(task: str, notes: Sequence[str], *, final: bool) -> str:
    joined = "\n\n".join(f"NOTES {i + 1}:\n{n}" for i, n in enumerate(notes))
    if final:
        return f"TASK: {task}\n\n{joined}\n\nAnswer the task from these notes:"
    return (
        f"TASK the final pass must complete: {task}\n\n{joined}\n\n"
        "Merge these notes into one set of notes for the final pass, keeping "
        "every fact and citation that could matter:"
    )


# A citation is a bracket holding one or more `source#idx` / `source#a-b` items, separated
# by `,` or `;` (a window spanning documents is cited as a comma list, `Window.cite`). A
# source holds no blank, comma, semicolon or bracket; a bracket with anything else in it
# is prose (`[PR #42]`, `[see doc#1]`) and is left as written. Of nested brackets, the
# innermost is the one read. An index is at most nine digits 0-9: a longer run is prose.
# One item, as (source, first index, last index); a citation is a list of them.
_ITEM_PARTS = re.compile(r"([^\s,;\[\]]+)#([0-9]{1,9})(?:-([0-9]{1,9}))?")
_ITEMS = re.compile(rf"{_ITEM_PARTS.pattern}(?:\s*[,;]\s*{_ITEM_PARTS.pattern})*")
# The blanks around a list's separators, which do not make it another citation.
_SEPARATOR = re.compile(r"\s*([,;])\s*")
# A removed citation goes with the blanks before it; the lookbehind starts the blanks at
# the start of their run, so a long run is scanned once.
_SPACED_CITATION = re.compile(rf"(?<![ \t])[ \t]*\[\s*({_ITEMS.pattern})\s*\]")
# A citation formed only after this many removals is left as written and unchecked: a
# model writes none, and each one is one more pass over the answer.
_MAX_PASSES = 8


def cites_what_was_read(tag: str, read: set[tuple[str, int]]) -> bool:
    """Whether `source#idx` (or `source#a-b`, a <= b, or a `,`/`;` list of them) names
    chunks in `read`, the (source, idx) pairs the passes were shown. A range needs both
    of its ends read, a list every part; a tag that is not such a list names nothing read."""
    if not _ITEMS.fullmatch(tag):
        return False
    for m in _ITEM_PARTS.finditer(tag):
        source, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        if not (a <= b and (source, a) in read and (source, b) in read):
            return False
    return True


def ground_citations(answer: str, read: set[tuple[str, int]]) -> tuple[str, list[str], list[str]]:
    """(the answer without the citations naming a chunk it did not read, the citations
    it keeps, the ones it removed, each in the order found). A model can cite a part it was
    never shown; a caller that follows it would read something the answer did not come
    from."""
    kept: list[str] = []
    removed: list[str] = []
    seen: set[str] = set()

    def check(m: re.Match[str]) -> str:
        tag = m.group(1)
        supported = cites_what_was_read(tag, read)
        key = _SEPARATOR.sub(r"\1", tag)
        if key not in seen:
            seen.add(key)
            (kept if supported else removed).append(tag)
        return m.group(0) if supported else ""

    # Removing an inner citation can close an outer bracket into a new one
    # (`[a#7[b#0]]`), so the pass repeats until it removes nothing; each nesting level
    # takes one pass, and past _MAX_PASSES levels the rest is left as written.
    for _ in range(_MAX_PASSES):
        grounded = _SPACED_CITATION.sub(check, answer)
        if grounded == answer:
            break
        answer = grounded
    return answer.strip(), kept, removed
