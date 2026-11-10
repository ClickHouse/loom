"""help.search and help.get wire models: Loom's own documentation as this build ships it,
the same for every caller."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Audience = Literal["user", "operator"]


class HelpSearchInput(BaseModel):
    """Search this build's documentation."""

    org: str = ""
    # What to find, in words: a question, a feature, an op or setting name.
    query: str = Field(min_length=1, max_length=500)
    # How many sections to return, best first.
    limit: int = Field(default=5, ge=1, le=10)


class HelpHit(BaseModel):
    # The page, relative to the docs root (`features/knowledge.md`).
    path: str
    # The page's title.
    title: str
    # The section's headings from the page title down, `Title > A > B`; empty for text
    # before the page's first heading.
    section: str
    # The section heading's anchor on the rendered page; empty for the page's opening.
    anchor: str
    # `user` for using Loom, `operator` for running or building it.
    audience: Audience
    # The section's text from its line that holds the most query terms, cut short.
    snippet: str
    # The section's BM25 score for the query; higher is better, comparable within one search.
    score: float


class HelpSearchOutput(BaseModel):
    # False when this build ships no documentation to read; `hits` is then empty.
    available: bool
    # The best sections, best first, at most `limit`.
    hits: list[HelpHit]


class HelpGetInput(BaseModel):
    """Read one page of this build's documentation, or one section of it."""

    org: str = ""
    # The page, as help.search returns it (`features/knowledge.md`).
    path: str = Field(min_length=1, max_length=300)
    # The section's headings, `Title > A > B`, as help.search returns it; empty for the
    # whole page.
    section: str = Field(default="", max_length=1000)
    # The section's anchor, as help.search returns it: read in place of `section`, since
    # it is unique on its page where a heading may repeat.
    anchor: str = Field(default="", max_length=300)
    # Where to start in the text read (the section, or the whole page), in characters: 0
    # by default; a cut read gives the offset its rest starts at.
    offset: int = Field(default=0, ge=0)


class HelpOutlineEntry(BaseModel):
    section: str
    anchor: str


class HelpGetOutput(BaseModel):
    # False when this build ships no documentation to read.
    available: bool
    # False when the page, or the section on it, is not in this build's documentation.
    found: bool
    # The page, relative to the docs root.
    path: str
    # The page's title.
    title: str = ""
    # The section read, `Title > A > B`; empty for the whole page.
    section: str = ""
    # The section heading's anchor on the rendered page; empty for the whole page.
    anchor: str = ""
    # `user` for using Loom, `operator` for running or building it.
    audience: Audience = "user"
    # The section with its subsections as Markdown, or the whole page, from `offset`; cut
    # at a limit.
    text: str = ""
    # True when `text` was cut; read again from `next_offset` for the rest.
    truncated: bool = False
    # Where the rest of a cut read starts; 0 when nothing was cut.
    next_offset: int = 0
    # Every section of the page, in order: for a whole-page read or a cut one, else empty.
    sections: list[HelpOutlineEntry] = Field(default_factory=list)
