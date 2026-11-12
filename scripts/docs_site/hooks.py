"""MkDocs hooks for the documentation site built from docs/.

Each page's meta description is its first sentence, the line llms.txt lists for it, so a
search result shows the answer the page opens with. A link that leaves docs/ (source
files, the README, llms.txt) points at the file on GitHub instead of a missing page.
robots.txt is written beside the sitemap.
"""

from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "codegen"))
from page_summary import page_summary  # noqa: E402

BLOB = "https://github.com/ClickHouse/loom/blob/main/"
_LINK = re.compile(r"(\]\()([^)\s#]+)(#[^)\s]*)?(\))")


def on_page_markdown(markdown, page, config, files):
    _title, summary = page_summary(Path(page.file.abs_src_path))
    if summary:
        page.meta.setdefault("description", summary)
    page_dir = posixpath.dirname("docs/" + page.file.src_uri)

    def outside(m: re.Match[str]) -> str:
        target = m.group(2)
        if "://" in target or target.startswith(("mailto:", "/")):
            return m.group(0)
        resolved = posixpath.normpath(posixpath.join(page_dir, target))
        if resolved.startswith("docs/"):
            return m.group(0)
        return f"{m.group(1)}{BLOB}{resolved}{m.group(3) or ''}{m.group(4)}"

    return _LINK.sub(outside, markdown)


def on_post_build(config):
    site = Path(config["site_dir"])
    url = config["site_url"].rstrip("/")
    (site / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {url}/sitemap.xml\n")
