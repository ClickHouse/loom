"""The citations an assistant answer writes, as its instructions ask for them:
`[memory:<key>@v<version>]`, `[doc:<source_uri> § <section>]` (the section optional),
`[help:<docs path> § <section>]` (a page of Loom's documentation, the section optional),
`[code:<path>:<line>]`, and each fenced `sql` block, which holds a statement exactly as
`insights.query` ran it. Pure: the checks against Loom are in the app layer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# A key may hold `@` (an email): it runs to the last `@v<version>]`.
_MEMORY = re.compile(r"\[memory:(?P<key>[^\]\s]+)@v(?P<version>\d+)\]")
# An optional `, revision <n>` note follows the section; any other comma is the section's.
_DOC = re.compile(
    r"\[doc:(?P<uri>[^\]§]+?)(?:\s*§\s*(?P<section>[^\]]+?))?\s*(?:,\s*revision\s+\d+)?\]"
)
_HELP = re.compile(r"\[help:(?P<path>[^\]§\s]+?)(?:\s*§\s*(?P<section>[^\]]+?))?\s*\]")
_CODE = re.compile(r"\[code:(?P<path>[^\]\s]+?):(?P<line>\d+)\]")
_SQL = re.compile(r"```sql[ \t]*\n(?P<sql>.*?)\n```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class Citation:
    """One citation: its kind (`memory`, `doc`, `help`, `code`, `sql`), its text as
    written, and what it names."""

    kind: Literal["memory", "doc", "help", "code", "sql"]
    text: str
    key: str = ""
    source_uri: str = ""
    section: tuple[str, ...] = ()
    path: str = ""
    sql: str = ""

    @property
    def ref(self) -> str:
        """What a reader opens: the memory key, the document's source, the docs page or the
        code path."""
        return {
            "memory": self.key,
            "doc": self.source_uri,
            "help": self.path,
            "code": self.path,
        }.get(self.kind, "")


def section_path(section: str) -> tuple[str, ...]:
    """A section breadcrumb (`A > B`) as its headings."""
    return tuple(part.strip() for part in section.split(">") if part.strip())


def parse(answer: str) -> list[Citation]:
    """Every distinct citation in `answer`, in the order it first appears."""
    found: list[tuple[int, Citation]] = []
    for m in _MEMORY.finditer(answer):
        found.append((m.start(), Citation("memory", m.group(0), key=m["key"])))
    for m in _DOC.finditer(answer):
        section = section_path(m["section"] or "")
        found.append(
            (m.start(), Citation("doc", m.group(0), source_uri=m["uri"].strip(), section=section))
        )
    for m in _HELP.finditer(answer):
        section = section_path(m["section"] or "")
        found.append((m.start(), Citation("help", m.group(0), path=m["path"], section=section)))
    for m in _CODE.finditer(answer):
        found.append((m.start(), Citation("code", m.group(0), path=m["path"])))
    for m in _SQL.finditer(answer):
        found.append((m.start(), Citation("sql", m.group(0), sql=m["sql"])))
    seen: set[str] = set()
    out: list[Citation] = []
    for _, c in sorted(found, key=lambda pair: pair[0]):
        if c.text not in seen:
            seen.add(c.text)
            out.append(c)
    return out
