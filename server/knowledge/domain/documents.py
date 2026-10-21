"""Document values the knowledge handlers and the download route share."""

from __future__ import annotations

import re

# The most hits one knowledge.search returns; readers that widen a search stay within it.
MAX_SEARCH_TOP_K = 100  # not a setting: a wire bound on knowledge.search


def text_download_filename(title: str, source_uri: str) -> str:
    """The file name a text-only document downloads under: a slug of the title, else of the
    last `source_uri` segment, with `.md`.

    ASCII letters, digits, `.`, `_` and `-` survive; every other run becomes
    one `-`. An existing `.md` suffix is kept rather than doubled; a title
    and URI that yield nothing fall back to `document.md`."""
    segment = source_uri.rstrip("/").rsplit("/", 1)[-1]
    for candidate in (title, segment):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-._")[:80]
        if slug:
            return slug if slug.lower().endswith(".md") else f"{slug}.md"
    return "document.md"
