"""Replace the legacy Pages HTML with links to the canonical documentation."""

from __future__ import annotations

import sys
from html import escape
from pathlib import Path
from urllib.parse import quote

DOCS_URL = "https://loom.clickhouse.com/docs"


def redirect_pages(site: Path) -> None:
    if not (site / "index.html").is_file():
        raise SystemExit(f"Missing documentation build at {site}")
    for page in site.rglob("index.html"):
        path = page.parent.relative_to(site).as_posix()
        target = DOCS_URL if path == "." else f"{DOCS_URL}/{quote(path)}"
        page.write_text(
            f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ClickHouse Loom documentation</title>
  <link rel="canonical" href="{escape(target)}">
  <meta http-equiv="refresh" content="0; url={escape(target)}">
</head>
<body>
  <p>Continue to the <a id="docs" href="{escape(target)}">ClickHouse Loom documentation</a>.</p>
  <script>
    const target = new URL(document.getElementById("docs").href);
    target.search = location.search;
    target.hash = location.hash;
    location.replace(target);
  </script>
</body>
</html>
""",
            encoding="utf-8",
        )


if __name__ == "__main__":
    redirect_pages(Path(sys.argv[1]))
