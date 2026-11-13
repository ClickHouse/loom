"""Tell IndexNow search engines which documentation-site pages a push to main changed.

IndexNow (Bing, Yandex, Seznam, Naver and others share one endpoint) takes a list of
changed URLs and proves ownership with a key file served on the site. The key file is the
one `.txt` file in docs/ whose name is its own content; it is public by design.

Usage: indexnow.py <changed path>...   (paths relative to the repository root)
A changed page under docs/ maps to its URL; a change to the site's build (mkdocs.yml, the
hooks or the theme overrides) changes every page, so the live sitemap is submitted whole.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

SITE = "https://clickhouse.github.io/loom/"
ENDPOINT = "https://api.indexnow.org/indexnow"
DOCS = Path(__file__).resolve().parents[2] / "docs"
BUILD_INPUTS = ("mkdocs.yml", "scripts/docs_site/")


def key() -> str:
    for p in DOCS.glob("*.txt"):
        if re.fullmatch(r"[0-9a-f]{32}", p.stem) and p.read_text().strip() == p.stem:
            return p.stem
    raise SystemExit("no IndexNow key file in docs/")


def page_url(path: str) -> str | None:
    """The site URL a changed docs/ Markdown file is served at, or None for other files."""
    if not (path.startswith("docs/") and path.endswith(".md")):
        return None
    rel = path[len("docs/") : -len(".md")]
    if rel == "index":
        return SITE
    if rel.endswith("/index"):
        rel = rel[: -len("/index")]
    return f"{SITE}{rel}/"


def sitemap_urls() -> list[str]:
    with urllib.request.urlopen(SITE + "sitemap.xml", timeout=30) as resp:
        return re.findall(r"<loc>([^<]+)</loc>", resp.read().decode())


def urls_for(paths: list[str]) -> list[str]:
    if any(p == i or p.startswith(i) for p in paths for i in BUILD_INPUTS):
        return sitemap_urls()
    return sorted({u for p in paths if (u := page_url(p))})


def payload(urls: list[str], k: str) -> dict:
    return {
        "host": "clickhouse.github.io",
        "key": k,
        "keyLocation": f"{SITE}{k}.txt",
        "urlList": urls,
    }


def main(paths: list[str]) -> int:
    urls = urls_for(paths)
    if not urls:
        print("indexnow: no documentation page changed")
        return 0
    body = json.dumps(payload(urls, key())).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"indexnow: {len(urls)} URL(s) submitted, HTTP {resp.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
