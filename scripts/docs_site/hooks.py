"""MkDocs hooks for the documentation site built from docs/.

Each page's meta description is its first sentence, the line llms.txt lists for it, so a
search result shows the answer the page opens with. An inline Markdown link or image that
leaves docs/ (source files, the README, llms.txt) points at the file on GitHub instead of
a missing page; links in code blocks and code spans are left as written, and
reference-style links are not rewritten (the docs use inline links). robots.txt is
written beside the sitemap.
"""

from __future__ import annotations

import html
import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.codegen.page_summary import page_summary  # noqa: E402

BLOB = "https://github.com/ClickHouse/loom/blob/main/"
RAW = "https://raw.githubusercontent.com/ClickHouse/loom/main/"
# Fenced blocks and code spans, kept verbatim; everything between them is rewritten.
_CODE = re.compile(r"^(```|~~~).*?^\1[^\n]*$|`[^`\n]+`", re.DOTALL | re.MULTILINE)
_LINK = re.compile(r"(!?)(\[[^\]]*\]\()([^)\s#]+)(#[^)\s]*)?((?:\s+\"[^\"]*\")?\))")


def on_page_markdown(markdown, page, config, files):
    _title, summary = page_summary(Path(page.file.abs_src_path))
    if summary:
        # Neither MkDocs nor the theme escapes the description into its attribute.
        page.meta.setdefault("description", html.escape(summary.replace("`", "")))
    page_dir = posixpath.dirname("docs/" + page.file.src_uri)

    def outside(m: re.Match[str]) -> str:
        bang, text, target, anchor, tail = m.groups()
        if "://" in target or target.startswith(("mailto:", "/")):
            return m.group(0)
        resolved = posixpath.normpath(posixpath.join(page_dir, target))
        if resolved.startswith("docs/"):
            return m.group(0)
        base = RAW if bang else BLOB
        return f"{bang}{text}{base}{resolved}{anchor or ''}{tail}"

    out, pos = [], 0
    for code in _CODE.finditer(markdown):
        out.append(_LINK.sub(outside, markdown[pos : code.start()]))
        out.append(code.group(0))
        pos = code.end()
    out.append(_LINK.sub(outside, markdown[pos:]))
    return "".join(out)


def on_post_build(config):
    site = Path(config["site_dir"])
    url = config["site_url"].rstrip("/")
    (site / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {url}/sitemap.xml\n")
