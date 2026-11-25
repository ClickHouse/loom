"""Tell IndexNow search engines which public site pages a push to main changed.

IndexNow search engines share one endpoint: it takes a list of changed URLs and checks
ownership against a key file served on the site. The key file is the one `.txt` file in
docs/ whose name is its own content; it is public by design.

Usage: indexnow.py --changes-file FILE [--previous-public-pages MAP]
A changes file comes from `git diff --name-status -z --no-renames`.
A changed or deleted Markdown or Astro page maps to its canonical URL. Changes to
shared render inputs submit the pages that use them. The previous map retains public
routes that a change to the map removes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path

ORIGIN = "https://loom.clickhouse.com"
SITE = f"{ORIGIN}/docs/"
ENDPOINT = "https://api.indexnow.org/indexnow"
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.docs_site.paths import page_path  # noqa: E402

DOCS = ROOT / "docs"
PUBLIC_PAGES_PATH = "docs/scripts/public-pages.json"
HOME_LOCALES_PATH = "docs/scripts/home-locales.json"
PUBLIC_PAGE_URLS = {
    f"docs/pages/{destination.removeprefix('/docs/')}.astro": ORIGIN + route
    for route, destination in json.loads((ROOT / PUBLIC_PAGES_PATH).read_text()).items()
}
HOME_URLS = {
    ORIGIN + page["href"] for page in json.loads((ROOT / HOME_LOCALES_PATH).read_text()).values()
}
SITE_BUILD_INPUTS = (
    "docs/blume.config.ts",
    "docs/lib/site-identity.mjs",
    "docs/package-lock.json",
    "docs/package.json",
    "docs/scripts/prepare-content.mjs",
    "docs/theme.css",
    "docs/vercel.json",
)
PUBLIC_BUILD_INPUTS = (
    *SITE_BUILD_INPUTS,
    "docs/components/",
    "docs/lib/adoption-data.mjs",
    "docs/lib/adoption-view.mjs",
    HOME_LOCALES_PATH,
    "docs/scripts/mount-site-pages.mjs",
    "docs/scripts/site-discovery.mjs",
    PUBLIC_PAGES_PATH,
)
BUILD_INPUTS = (
    *SITE_BUILD_INPUTS,
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
    """The canonical URL of a public page source, if it has one."""
    if path in PUBLIC_PAGE_URLS:
        return PUBLIC_PAGE_URLS[path]
    if path.startswith("i18n/"):
        from scripts.docs_site.locales import localized_page_path

        localized = localized_page_path(path)
        return SITE + localized if localized is not None else None
    if not (path.startswith("docs/") and path.endswith(".md")):
        return None
    return SITE + urllib.parse.quote(page_path(path))


def every_page() -> list[str]:
    """The published documentation routes, including translations."""
    from scripts.docs_site.locales import translations

    paths = [p.relative_to(DOCS) for p in DOCS.rglob("*.md")]
    english = [
        url
        for path in paths
        if not path.parts[0].startswith(".")
        and path.parts[0] not in {"dist", "node_modules"}
        and (url := page_url(f"docs/{path.as_posix()}"))
    ]
    localized = [
        SITE + urllib.parse.quote(page_path(f"docs/{uri}")) for uri in translations(DOCS.parent)
    ]
    return sorted([*english, *localized])


def urls_for(changes: Mapping[str, str], previous_public_routes: Iterable[str] = ()) -> list[str]:
    from scripts.docs_site.locales import LANGUAGES, localized_page_path

    changed = {u for p in changes if (u := page_url(p))}
    topology = [p for p, status in changes.items() if status != "M"]
    # When a route appears or disappears, alternate sets and translated links can change.
    locale_change = any(localized_page_path(p) is not None for p in topology) or (
        any(p.startswith("docs/") and p.endswith(".md") for p in topology)
        and any(any((DOCS.parent / "i18n" / lang / "docs").rglob("*.md")) for lang in LANGUAGES)
    )
    if locale_change or _changed_build_input(changes, BUILD_INPUTS):
        # A deleted page is no longer in the tree, so keep the changed paths' URLs too.
        changed.update(every_page())
    if locale_change:
        # Homepage links switch when a translation appears or disappears.
        changed.update(HOME_URLS)
    if _changed_build_input(changes, PUBLIC_BUILD_INPUTS):
        changed.update(PUBLIC_PAGE_URLS.values())
    if PUBLIC_PAGES_PATH in changes:
        changed.update(ORIGIN + route for route in previous_public_routes)
    return sorted(changed)


def _changed_build_input(paths: Iterable[str], inputs: tuple[str, ...]) -> bool:
    return any(
        path == item or (item.endswith("/") and path.startswith(item))
        for path in paths
        for item in inputs
    )


def payload(urls: list[str], k: str) -> dict:
    return {
        "host": urllib.parse.urlsplit(SITE).hostname,
        "key": k,
        "keyLocation": urllib.parse.urljoin(SITE, f"/{k}.txt"),
        "urlList": [url if url == f"{ORIGIN}/" else url.rstrip("/") for url in urls],
    }


def read_changes(path: Path) -> dict[str, str]:
    data = path.read_bytes()
    if not data:
        return {}
    fields = data.removesuffix(b"\0").split(b"\0")
    return {
        os.fsdecode(name): status.decode("ascii")
        for status, name in zip(fields[::2], fields[1::2], strict=True)
    }


def main(
    changes: Mapping[str, str],
    previous_public_pages: Path | None = None,
    *,
    list_urls: bool = False,
) -> int:
    previous_routes = (
        json.loads(previous_public_pages.read_text())
        if previous_public_pages is not None and PUBLIC_PAGES_PATH in changes
        else ()
    )
    urls = urls_for(changes, previous_routes)
    if list_urls:
        if urls:
            print("\n".join(urls))
        return 0
    if not urls:
        print("indexnow: no public page changed")
        return 0
    body = json.dumps(payload(urls, key())).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"indexnow: {len(urls)} URL(s) submitted, HTTP {resp.status}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List changed URLs without submitting.")
    parser.add_argument("--previous-public-pages", type=Path)
    parser.add_argument("--changes-file", type=Path, required=True)
    arguments = parser.parse_args()
    sys.exit(
        main(
            read_changes(arguments.changes_file),
            arguments.previous_public_pages,
            list_urls=arguments.list,
        )
    )
