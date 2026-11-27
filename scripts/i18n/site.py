"""Documentation-site routes used when generated README links name a translation."""

from __future__ import annotations

from collections.abc import Iterable

SITE = "https://loom.clickhouse.com/docs/"
DOCUMENTATION = {
    "en": "Documentation",
    "zh-CN": "文档",
    "ja": "ドキュメント",
    "ko": "문서",
    "pt-BR": "Documentação",
    "es": "Documentación",
}


def landing_sources(sources: Iterable[str]) -> list[str]:
    """Prefer documentation indexes and quickstart before other available pages."""
    priority = {"index.md": 0, "use-cases/index.md": 1, "getting-started/quickstart.md": 2}
    return sorted(set(sources), key=lambda source: (priority.get(source, 3), source))


def page_path(path: str) -> str:
    """Return the path under the site for a docs-relative Markdown file."""
    rel = path[len("docs/") : -len(".md")]
    if rel == "index":
        return ""
    if rel.endswith("/index"):
        rel = rel[: -len("/index")]
    return f"{rel}/"
