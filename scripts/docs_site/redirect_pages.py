"""Point legacy Pages HTML and agent exports at the canonical documentation."""

from __future__ import annotations

import json
import re
import sys
from html import escape
from pathlib import Path
from urllib.parse import quote

from scripts.docs_site.hooks import _TOKEN

DOCS_URL = "https://loom.clickhouse.com/docs"
# The fragment map inverts the pins reported by Blume's pin-heading-ids.mjs
# when comparing the legacy and canonical site builds.
FRAGMENT_MAP = Path(__file__).with_name("legacy_fragments.json")


def _export_destinations(legacy_site_url: str, route: str, target: str) -> dict[str, str]:
    old = legacy_site_url.rstrip("/") + quote(route).rstrip("/")
    markdown = f"{target}.md" if route != "/" else f"{target}/index.md"
    return {
        old: target,
        f"{old}/": target,
        f"{old}/index.html": target,
        f"{old}/index.md": markdown,
        f"{old}/index.html.md": markdown,
        f"{old}.md" if route != "/" else f"{old}/index.md": markdown,
    }


def _retarget_exports(site: Path, legacy_site_url: str, destinations: dict[str, str]) -> None:
    legacy = re.escape(legacy_site_url.rstrip("/") + "/")
    urls = re.compile(rf"{_TOKEN.pattern}|(?P<bare_url>{legacy}[^\s<>\[\]#?()\"`]*)", _TOKEN.flags)

    def replace(match: re.Match[str]) -> str:
        if match.group("link"):
            target, separator, query = match.group("target").partition("?")
            destination = destinations.get(target)
            if destination is not None:
                start, end = match.span("target")
                return (
                    match[0][: start - match.start()]
                    + destination
                    + separator
                    + query
                    + match[0][end - match.start() :]
                )
        elif bare_url := match.group("bare_url"):
            return destinations.get(bare_url, bare_url)
        return match[0]

    for path in (*site.rglob("*.md"), site / "llms.txt", site / "llms-full.txt"):
        if path.is_file():
            original = path.read_text(encoding="utf-8")
            updated = urls.sub(replace, original)
            if updated != original:
                path.write_text(updated, encoding="utf-8")


def redirect_pages(site: Path, legacy_site_url: str) -> None:
    if not (site / "index.html").is_file():
        raise SystemExit(f"Missing documentation build at {site}")
    fragments = json.loads(FRAGMENT_MAP.read_text(encoding="utf-8"))
    destinations = {
        f"{legacy_site_url.rstrip('/')}/{name}": f"{DOCS_URL}/{name}"
        for name in ("llms.txt", "llms-full.txt", "llms-install.md")
    }
    for page in site.rglob("index.html"):
        path = page.parent.relative_to(site).as_posix()
        route = "/" if path == "." else f"/{path}"
        target = DOCS_URL + quote(route.rstrip("/"))
        destinations.update(_export_destinations(legacy_site_url, route, target))
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
    (site / "404.html").write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex">
  <title>Page not found | ClickHouse Loom</title>
</head>
<body>
  <main>
    <h1>Page not found</h1>
    <p>Find the page in the <a href="{escape(DOCS_URL)}">ClickHouse Loom documentation</a>.</p>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    _retarget_exports(site, legacy_site_url, destinations)


if __name__ == "__main__":
    redirect_pages(Path(sys.argv[1]), sys.argv[2])
