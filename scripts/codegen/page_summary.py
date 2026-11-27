"""A documentation page's title and the first sentence of its opening paragraph.

The documentation site uses this sentence as the page's meta description, cut to the
length a search engine shows. Standard library only: the site build imports it without the
server's dependencies.
"""

from __future__ import annotations

import re
from pathlib import Path

# The docs sections, as (title, folder under docs/), in the documentation site's sidebar order.
SECTIONS = (
    ("Getting started", "getting-started"),
    ("Use cases", "use-cases"),
    ("Operations", "operations"),
    ("Features", "features"),
    ("Reference", "reference"),
    ("Concepts", "concepts"),
    ("Guides", "guides"),
    ("Architecture", "architecture"),
    ("Benchmarks", "benchmarks"),
    ("Development", "development"),
)

# The pages that open their section right after its index page (Getting started's is its
# overview.md): the documentation site's sidebar lists them in this order.
LEAD_PAGES = ("getting-started/quickstart.md",)


def page_key(uri: str) -> tuple[str, int, int, str]:
    """Sort key for a page (path under docs/) within its section: the section's own pages,
    then each subfolder's by folder name; within a folder, its index page (`index.md` or
    `overview.md`; no folder has both), then `LEAD_PAGES`, then the rest by path."""
    sub, _, name = uri.partition("/")[2].rpartition("/")
    lead = LEAD_PAGES.index(uri) if uri in LEAD_PAGES else len(LEAD_PAGES)
    return (sub, int(name not in ("index.md", "overview.md")), lead, uri)


def shorten(text: str, limit: int) -> str:
    """`text` if it fits in `limit` characters, else the words from its start that fit
    (a first word longer than the limit is cut), trailing punctuation dropped, plus "…"."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    if text[limit - 1] not in " ,;:":  # a word that ends at a space or punctuation fits
        head = head.rsplit(" ", 1)[0]  # the slice ends inside a word: drop that word
    return head.rstrip(",;:—-/ ") + "…"


def first_sentence(text: str | None, limit: int = 200) -> str:
    """The first sentence of a docstring or description, on one line."""
    if not text:
        return ""
    para = " ".join(text.strip().split("\n\n")[0].split())
    # Not after "e.g." or "i.e.", which never end a sentence ("etc." may).
    m = re.search(r"(?<!\.\.)(?<![eE]\.g\.)(?<![iI]\.e\.)(?<=[.!?])\s+(?=[A-Z`*(])", para)
    sentence = para[: m.start()] if m else para
    sentence = sentence.replace("**", "")
    return shorten(sentence, limit)


def page_summary(path: Path) -> tuple[str, str]:
    """A page's title and the first sentence of its opening paragraph (the first plain
    paragraph after the title: not a heading, status note, list, table or code block)."""
    text = path.read_text(encoding="utf-8")
    title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
    body = re.sub(r"^(```|~~~).*?^\1", "", text, flags=re.DOTALL | re.MULTILINE)
    summary = ""
    for para in re.split(r"\n\s*\n", body):
        first = para.lstrip()
        if not first or first[0] in "#>|-*<!" or first[:2].isdigit() or first.startswith("1."):
            continue
        # Links are page-relative: keep their text, before the sentence is cut to length.
        plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", " ".join(para.split()))
        summary = first_sentence(plain, 240)
        break
    return title.replace("`", ""), summary
