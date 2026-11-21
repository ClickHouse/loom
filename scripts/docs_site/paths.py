"""Routes shared by the HTML, Markdown and IndexNow documentation outputs."""

from __future__ import annotations

from collections.abc import Iterable


def landing_sources(sources: Iterable[str]) -> list[str]:
    """Prefer documentation indexes and quickstart before other available pages."""
    priority = {"index.md": 0, "use-cases/index.md": 1, "getting-started/quickstart.md": 2}
    return sorted(set(sources), key=lambda source: (priority.get(source, 3), source))


def page_path(path: str) -> str:
    """The path under the site a docs/ Markdown file is served at ("" for the home page)."""
    rel = path[len("docs/") : -len(".md")]
    if rel == "index":
        return ""
    if rel.endswith("/index"):
        rel = rel[: -len("/index")]
    return f"{rel}/"
