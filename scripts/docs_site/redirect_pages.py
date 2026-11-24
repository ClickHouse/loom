"""Replace the legacy Pages HTML with links to the canonical documentation."""

from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path
from urllib.parse import quote

DOCS_URL = "https://loom.clickhouse.com/docs"
# The fragment map inverts the pins reported by Blume's pin-heading-ids.mjs
# when comparing the legacy and canonical site builds.
FRAGMENT_MAP = Path(__file__).with_name("legacy_fragments.json")


def redirect_pages(site: Path) -> None:
    if not (site / "index.html").is_file():
        raise SystemExit(f"Missing documentation build at {site}")
    fragments = json.loads(FRAGMENT_MAP.read_text(encoding="utf-8"))
    for page in site.rglob("index.html"):
        path = page.parent.relative_to(site).as_posix()
        route = "/" if path == "." else f"/{path}"
        target = DOCS_URL + quote(route.rstrip("/"))
        aliases = json.dumps(list(fragments.get(route, {}).items())).replace("<", r"\u003c")
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
    const aliases = new Map({aliases});
    if (location.hash && aliases.size) {{
      let fragment = location.hash.slice(1);
      try {{
        fragment = decodeURIComponent(fragment);
      }} catch {{
        // A malformed escape can still be part of a literal fragment.
      }}
      target.hash = aliases.get(fragment) ?? location.hash;
    }}
    location.replace(target);
  </script>
</body>
</html>
""",
            encoding="utf-8",
        )


if __name__ == "__main__":
    redirect_pages(Path(sys.argv[1]))
