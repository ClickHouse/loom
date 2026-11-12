"""The documentation site's MkDocs hooks: meta description and links that leave docs/."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from scripts.codegen.page_summary import first_sentence, page_summary

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "docs_site_hooks", ROOT / "scripts/docs_site/hooks.py"
)
assert _SPEC and _SPEC.loader
hooks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hooks)


def _page(src_uri: str) -> SimpleNamespace:
    path = ROOT / "docs" / src_uri
    return SimpleNamespace(file=SimpleNamespace(abs_src_path=str(path), src_uri=src_uri), meta={})


def test_a_page_gets_its_first_sentence_as_description() -> None:
    page = _page("use-cases/agent-memory.md")
    hooks.on_page_markdown("", page, None, None)
    assert page.meta["description"].startswith("Loom is a self-hosted MCP server")


def test_links_outside_docs_point_at_github_and_others_stay() -> None:
    page = _page("features/runs.md")
    out = hooks.on_page_markdown(
        "[a](../../server/x.py#L1) [b](memory/writing.md#key) [c](https://example.com/y)",
        page,
        None,
        None,
    )
    assert "[a](https://github.com/ClickHouse/loom/blob/main/server/x.py#L1)" in out
    assert "[b](memory/writing.md#key)" in out
    assert "[c](https://example.com/y)" in out


def test_robots_txt_allows_crawlers_and_names_the_sitemap(tmp_path: Path) -> None:
    hooks.on_post_build({"site_dir": str(tmp_path), "site_url": "https://example.com/docs/"})
    robots = (tmp_path / "robots.txt").read_text()
    assert "User-agent: *\nAllow: /" in robots
    assert "Sitemap: https://example.com/docs/sitemap.xml" in robots


def test_the_summary_is_the_title_and_first_plain_sentence() -> None:
    title, summary = page_summary(ROOT / "docs/use-cases/index.md")
    assert title == "Use cases"
    assert summary == first_sentence(summary, 240)
    assert summary.startswith("Each page answers one question")
