"""MkDocs hooks for the documentation site built from docs/.

Each page's meta description is its first sentence, the line llms.txt lists for it, so a
search result shows the answer the page opens with. An inline Markdown link or image that
leaves docs/ (source files, the README, llms.txt) points at the file on GitHub instead of
a missing page; links in code blocks and code spans are left as written, and
reference-style links are not rewritten (the docs use inline links).
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
# Scanned left to right: a fenced block (indented ones too) or a code span is kept as
# written, an inline link or image may be rewritten. A link's text may hold code spans;
# an image inside a link is matched as the image.
_TOKEN = re.compile(
    r"(?P<fence>^[ \t]*(?P<mark>```|~~~)[^\n]*\n.*?^[ \t]*(?P=mark)[^\n]*$)"
    r"|(?P<link>(?P<bang>!?)(?P<text>\[(?:`[^`\n]*`|!(?!\[)|[^\]`!])*\]\()"
    r"(?P<target>[^)\s#]+)(?P<anchor>#[^)\s]*)?(?P<tail>(?:\s+\"[^\"]*\")?\)))"
    r"|(?P<code>`[^`\n]+`)",
    re.DOTALL | re.MULTILINE,
)


def on_page_markdown(markdown, page, config, files):
    _title, summary = page_summary(Path(page.file.abs_src_path))
    description = page.meta.get("description") or summary.replace("`", "")
    if description:
        # The theme writes the description into its <meta> attribute unescaped.
        page.meta["description"] = html.escape(description)
    page_dir = posixpath.dirname("docs/" + page.file.src_uri)

    def rewrite(m: re.Match[str]) -> str:
        if not m.group("link"):
            return m.group(0)
        target = m.group("target")
        if "://" in target or target.startswith(("mailto:", "/")):
            return m.group(0)
        resolved = posixpath.normpath(posixpath.join(page_dir, target))
        if resolved.startswith("docs/"):
            return m.group(0)
        base = RAW if m.group("bang") else BLOB
        anchor = m.group("anchor") or ""
        return f"{m.group('bang')}{m.group('text')}{base}{resolved}{anchor}{m.group('tail')}"

    return _TOKEN.sub(rewrite, markdown)
