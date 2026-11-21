"""Tell IndexNow search engines which documentation-site pages a push to main changed.

IndexNow search engines share one endpoint: it takes a list of changed URLs and checks
ownership against a key file served on the site. The key file is the one `.txt` file in
docs/ whose name is its own content; it is public by design.

Usage: indexnow.py <changed path>...   (paths relative to the repository root)
A changed or deleted page under docs/ maps to its URL. A change to what renders the pages
(mkdocs.yml, the hooks, the shared routes, the theme overrides, the pinned builder, or
scripts/codegen/page_summary.py, which supplies the descriptions and the sidebar order)
changes every page, so every page under docs/ is submitted; a change to this script alone
submits none.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SITE = "https://clickhouse.github.io/loom/"
ENDPOINT = "https://api.indexnow.org/indexnow"
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.docs_site.paths import page_path  # noqa: E402

DOCS = ROOT / "docs"
BUILD_INPUTS = (
    "mkdocs.yml",
    "scripts/codegen/page_summary.py",
    "scripts/docs_site/hooks.py",
    "scripts/docs_site/locales.py",
    "scripts/docs_site/messages.py",
    "scripts/docs_site/video_labels.py",
    "scripts/docs_site/paths.py",
    "scripts/docs_site/requirements.txt",
    "scripts/docs_site/overrides/",
)


def key() -> str:
    for p in DOCS.glob("*.txt"):
        if re.fullmatch(r"[0-9a-f]{32}", p.stem) and p.read_text().strip() == p.stem:
            return p.stem
    raise SystemExit("no IndexNow key file in docs/")


def page_url(path: str) -> str | None:
    """The site URL a docs/ Markdown file is served at, or None for other files."""
    if path.startswith("i18n/"):
        from scripts.docs_site.locales import localized_page_path

        localized = localized_page_path(path)
        return SITE + localized if localized is not None else None
    if not (path.startswith("docs/") and path.endswith(".md")):
        return None
    return SITE + urllib.parse.quote(page_path(path))


def every_page() -> list[str]:
    from scripts.docs_site.locales import translations

    paths = [f"docs/{p.relative_to(DOCS).as_posix()}" for p in DOCS.rglob("*.md")]
    english = [u for p in paths if (u := page_url(p))]
    localized = [
        SITE + urllib.parse.quote(page_path(f"docs/{uri}")) for uri in translations(DOCS.parent)
    ]
    return sorted([*english, *localized])


def urls_for(paths: list[str]) -> list[str]:
    from scripts.docs_site.locales import LANGUAGES, localized_page_path

    changed = {u for p in paths if (u := page_url(p))}
    english = [p for p in paths if p.startswith("docs/") and p.endswith(".md")]
    for source in english:
        for language in LANGUAGES:
            path = f"i18n/{language}/docs/{source[len('docs/') :]}"
            if (DOCS.parent / path).is_file() and (url := page_url(path)):
                changed.add(url)
    # Translation links depend on target existence, and alternate sets and source
    # notices depend on their peers. Include retained orphans when a source is deleted.
    locale_change = any(localized_page_path(p) is not None for p in paths) or (
        english
        and any(any((DOCS.parent / "i18n" / lang / "docs").rglob("*.md")) for lang in LANGUAGES)
    )
    if locale_change or any(
        p == i or (i.endswith("/") and p.startswith(i)) for p in paths for i in BUILD_INPUTS
    ):
        # A deleted page is no longer in the tree, so keep the changed paths' URLs too.
        return sorted(changed | set(every_page()))
    return sorted(changed)


def payload(urls: list[str], k: str) -> dict:
    return {
        "host": urllib.parse.urlsplit(SITE).hostname,
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
