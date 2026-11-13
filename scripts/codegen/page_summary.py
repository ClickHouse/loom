"""A documentation page's title and the first sentence of its opening paragraph.

`llms.txt` (scripts/codegen/docs_reference.py) lists this sentence for every page, and the
documentation site (scripts/docs_site/hooks.py) uses it as the page's meta description,
cut to the length a search engine shows, so both start with the same answer. Standard
library only: the site build imports it without the server's dependencies.
"""

from __future__ import annotations

import re
from pathlib import Path

# The docs sections in reading order, as (title, folder under docs/): llms.txt lists them
# in this order and the documentation site's sidebar follows it.
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


def shorten(text: str, limit: int) -> str:
    """`text` if it fits in `limit` characters, else its last whole words that fit, plus "…"."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(",;:—- ") + "…"


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
