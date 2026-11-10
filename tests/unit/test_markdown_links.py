"""The Markdown page helper: links outside fences and GitHub's heading anchors."""

from __future__ import annotations

from pathlib import Path

from tests.unit.markdown_links import anchors, links

PAGE = """# Install

## Install

## The <code>loom</code> CLI

[guide](./guide.md) and ![logo](./logo.png) and [titled](./t.md "Title")

```sh
[not a link](./fenced.md)
```
"""


def test_a_repeated_heading_gets_a_numbered_anchor_and_html_is_dropped(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(PAGE)
    assert anchors(page) == {"install", "install-1", "the-loom-cli"}


def test_links_skip_images_and_fenced_code(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(PAGE)
    assert links(page) == ["./guide.md", "./t.md"]
