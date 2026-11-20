"""Loom's documentation pages split into sections, and a BM25 index over them. Pure: the
pages come in as text, read by the app layer.

A section starts at a Markdown heading and holds the text up to the next heading. Its
breadcrumb is its headings from the page's title down (`Title > A > B`), its anchor the
slug a Markdown renderer gives its heading, as the docs' own links name it."""

from __future__ import annotations

import re
from array import array
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from server.core import bm25
from server.knowledge.domain.chunking import Section as PageSection
from server.knowledge.domain.chunking import detect_sections

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# ASCII whitespace and punctuation except `~`, as ClickHouse's splitByNonAlpha splits:
# `memory.set` is two terms, `LOOM_AUTH_REQUIRED` three.
_SPLIT = re.compile(r"[\s!-/:-@\[-`{-}]+")
# A whole identifier (`memory.set`, `loom_assistant_model`, `read_scope`) is a term too,
# so an exact op or setting name finds the line that names it.
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:[._][a-z0-9]+)+")
# A query term this short (`a`, `I`) says nothing about a page.
_MIN_TERM = 2
# A query past this many terms is cut: each term reads its postings once.
MAX_QUERY_TERMS = 20  # not a setting: bounds one search's work on the event loop
SNIPPET_CHARS = 400  # not a setting: the size of one hit an agent reads
# The heading's own terms count this many times over the section's text.
_HEADING_WEIGHT = 3

# Directories whose pages describe running and building Loom rather than using it.
_OPERATOR_DIRS = ("operations/", "architecture/", "benchmarks/")
# An operator section's score is scaled by this: most askers use Loom rather than run it,
# and a design page repeats the words of the user page it explains.
_OPERATOR_WEIGHT = 0.75


@dataclass(frozen=True)
class Section:
    path: str
    title: str
    breadcrumb: tuple[str, ...]
    anchor: str
    text: str

    @property
    def section(self) -> str:
        return " > ".join(self.breadcrumb)

    @property
    def heading(self) -> str:
        """The section's heading as the page writes it (its first line, an ATX heading's
        `#`s left off); empty for text before the page's first heading."""
        if not self.breadcrumb:
            return ""
        first = self.text.split("\n", 1)[0]
        m = _HEADING.match(first)
        return m[2] if m else first.strip()

    @property
    def audience(self) -> Literal["user", "operator"]:
        return "operator" if self.path.startswith(_OPERATOR_DIRS) else "user"


@dataclass
class Index:
    sections: list[Section] = field(default_factory=list)
    # Per page, its sections' ids in page order.
    pages: dict[str, list[int]] = field(default_factory=dict)
    # Per term, the ids of the sections holding it and how often, as two parallel arrays.
    postings: dict[str, tuple[array[int], array[int]]] = field(default_factory=dict)
    lengths: array[int] = field(default_factory=lambda: array("I"))
    avgdl: float = 1.0
    # Every whole identifier the pages write (`memory.set`, `loom_client_id`), lower-cased.
    identifiers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Hit:
    section: Section
    score: float
    snippet: str


def slug(heading: str) -> str:
    """A heading's anchor, as GitHub renders it and `tests/unit/test_doc_links.py` checks."""
    text = re.sub(r"<[^>]+>", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _fold(term: str) -> str:
    """A light stem, the same for the pages and the query: `keys` folds to `key`,
    `sharing`, `shared` and `shares` to `shar`, `settings` and `setting` to `set`,
    `embeds`, `embedding` and `embedded` to `embed`. Not English-exact; only the two
    sides' agreement matters."""
    if len(term) <= 3 or not term.isalpha() or term.endswith(("ss", "us", "is")):
        return term
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if term.endswith("s"):
        # A plural, then whatever its singular folds to (`settings` -> `setting` -> `set`).
        return _fold(term[:-2] if term.endswith(("ches", "shes", "xes")) else term[:-1])
    if len(term) <= 4:
        return term
    for suffix in ("ing", "ed", "e"):
        stem = term[: -len(suffix)]
        if term.endswith(suffix) and len(stem) >= 4:
            if stem[-1] == stem[-2] and stem[-1] not in "lsz":
                stem = stem[:-1]  # embedding -> embedd -> embed
            # `aliases` -> `aliase` -> `alias`, then as `alias` folds.
            return _fold(stem) if stem.endswith("s") else stem
    return term


# Words a question is phrased with, which say nothing about which page answers it.
_STOP = frozenset(
    """a about after all also an and any are as at be been before but by can could do
    does doing did for from get got has have how i if in into is it its me my no not
    of on or our please should so that the their them then there these they this to
    us use using was we were what when where which who why will with would you your""".split()
)


def terms(text: str) -> list[str]:
    """Every term of `text`, repeats kept: its words, folded, then its whole identifiers."""
    lowered = text.lower()
    words = [_fold(t) for t in _SPLIT.split(lowered) if len(t) >= _MIN_TERM]
    return words + _IDENTIFIER.findall(lowered)


def query_terms(query: str) -> list[str]:
    """The query's distinct terms but its question words, in order, at most
    MAX_QUERY_TERMS: its whole identifiers first, then its words; a query of question
    words only keeps them."""
    lowered = query.lower()
    raw = [t for t in _SPLIT.split(lowered) if len(t) >= _MIN_TERM]
    kept = [t for t in raw if t not in _STOP] or raw
    out = _IDENTIFIER.findall(lowered) + [_fold(t) for t in kept]
    return list(dict.fromkeys(out))[:MAX_QUERY_TERMS]


def split_page(path: str, text: str) -> list[Section]:
    """`text`'s sections as the document plane splits them (`detect_sections`): text
    before the first heading, if any, with an empty breadcrumb, then one per heading,
    whose breadcrumb starts with the page's title. Each section's text starts with its
    heading line."""
    found = detect_sections(text)
    if not found:
        found = [PageSection(path=(), depth=0, start=0, end=len(text))]
    title = next((s.path[0] for s in found if s.path), path.rsplit("/", 1)[-1].removesuffix(".md"))
    seen: dict[str, int] = {}
    out: list[Section] = []
    begin = 0
    for s in found:
        body = text[begin : s.end].strip()
        begin = s.end
        anchor = ""
        if s.path:
            base = slug(s.path[-1])
            n = seen.get(base, 0)
            seen[base] = n + 1
            anchor = base if n == 0 else f"{base}-{n}"
        if s.path or body:
            out.append(Section(path, title, tuple(s.path), anchor, body))
    return out


def build(pages: Iterable[tuple[str, str]]) -> Index:
    """The index over every `(path, text)` page."""
    index = Index()
    counts: dict[str, dict[int, int]] = {}
    for path, text in pages:
        for section in split_page(path, text):
            sid = len(index.sections)
            index.sections.append(section)
            index.pages.setdefault(path, []).append(sid)
            tf = Counter(terms(section.text))
            heading = section.breadcrumb[-1] if section.breadcrumb else section.title
            for t in terms(heading):
                tf[t] += _HEADING_WEIGHT
            for t in terms(section.title):
                tf[t] += 1
            index.lengths.append(sum(tf.values()))
            for t, n in tf.items():
                counts.setdefault(t, {})[sid] = n
    for t, by_section in counts.items():
        index.postings[t] = (array("I", by_section.keys()), array("I", by_section.values()))
    index.identifiers = frozenset(t for t in index.postings if _IDENTIFIER.fullmatch(t))
    if index.lengths:
        index.avgdl = max(sum(index.lengths) / len(index.lengths), 1.0)
    return index


def search(index: Index, query: str, limit: int) -> list[Hit]:
    """The `limit` best sections for `query`, best first; ties keep page order."""
    wanted = query_terms(query)
    n = len(index.sections)
    scores: dict[int, float] = {}
    for t in wanted:
        posting = index.postings.get(t)
        if posting is None:
            continue
        ids, tfs = posting
        idf = bm25.idf(len(ids), n)
        for sid, tf in zip(ids, tfs, strict=True):
            scores[sid] = scores.get(sid, 0.0) + bm25.term(
                tf, idf, dl=index.lengths[sid], avgdl=index.avgdl
            )
    for sid in scores:
        if index.sections[sid].audience == "operator":
            scores[sid] *= _OPERATOR_WEIGHT
    best = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [
        Hit(index.sections[sid], round(score, 4), snippet(index.sections[sid].text, wanted))
        for sid, score in best
    ]


def snippet(text: str, wanted: list[str]) -> str:
    """The section from its line that holds the most query terms (a whole identifier
    counting as three), cut to SNIPPET_CHARS; from its start when no line holds one."""
    lines = text.splitlines()
    want = set(wanted)
    start, best = 0, 0
    for i, line in enumerate(lines):
        if _HEADING.match(line):
            continue
        held = want.intersection(terms(line))
        score = sum(3 if _IDENTIFIER.fullmatch(t) else 1 for t in held)
        if score > best:
            start, best = i, score
    out = " ".join(" ".join(lines[start:]).split())
    return out if len(out) <= SNIPPET_CHARS else out[: SNIPPET_CHARS - 1].rstrip() + "…"


def section_path(section: str) -> tuple[str, ...]:
    """A breadcrumb (`A > B`) as its headings."""
    return tuple(part.strip() for part in section.split(">") if part.strip())


def page_sections(index: Index, path: str) -> list[Section]:
    return [index.sections[i] for i in index.pages.get(path, [])]


def find_section(index: Index, path: str, breadcrumb: tuple[str, ...]) -> Section | None:
    """The section of `path` a cited `breadcrumb` names, or None: the one it equals, else
    the one it equals once the page title is left off, else the only one it ends. A
    citation that writes a heading alone (`Daily spend caps`) names its section as long as
    no other section of the page ends the same way."""
    if not breadcrumb:
        return None
    sections = page_sections(index, path)
    exact = next((s for s in sections if s.breadcrumb == breadcrumb), None)
    if exact is not None:
        return exact
    untitled = next((s for s in sections if s.breadcrumb[1:] == breadcrumb), None)
    if untitled is not None:
        return untitled
    tails = [s for s in sections if s.breadcrumb[-len(breadcrumb) :] == breadcrumb]
    return tails[0] if len(tails) == 1 else None


def section_text(
    index: Index, path: str, breadcrumb: tuple[str, ...], anchor: str = ""
) -> Section | None:
    """The section at `anchor`, else at `breadcrumb` (its first, should a page repeat a
    heading), with its subsections' text joined in, or None. No anchor and the empty
    breadcrumb are the whole page."""
    sections = page_sections(index, path)
    if not sections:
        return None
    if not anchor and not breadcrumb:
        first = sections[0]
        text = "\n\n".join(s.text for s in sections if s.text)
        return Section(path, first.title, (), "", text)
    at = next(
        (
            i
            for i, s in enumerate(sections)
            if (s.anchor == anchor if anchor else s.breadcrumb == breadcrumb)
        ),
        None,
    )
    if at is None:
        return None
    s = sections[at]
    depth = len(s.breadcrumb)
    parts = [s.text]
    for sub in sections[at + 1 :]:
        if len(sub.breadcrumb) <= depth or sub.breadcrumb[:depth] != s.breadcrumb:
            break
        parts.append(sub.text)
    return Section(s.path, s.title, s.breadcrumb, s.anchor, "\n\n".join(p for p in parts if p))


__all__ = [
    "MAX_QUERY_TERMS",
    "SNIPPET_CHARS",
    "Hit",
    "Index",
    "Section",
    "build",
    "find_section",
    "page_sections",
    "query_terms",
    "search",
    "section_path",
    "section_text",
    "slug",
    "split_page",
    "terms",
]
