"""The documentation site's IndexNow submission: which URLs a push changed, and the key."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("indexnow", ROOT / "scripts/docs_site/indexnow.py")
assert _SPEC and _SPEC.loader
indexnow = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(indexnow)

SITE = "https://clickhouse.github.io/loom/"


def test_a_changed_page_maps_to_the_url_the_site_serves_it_at() -> None:
    assert indexnow.page_url("docs/use-cases/agent-memory.md") == SITE + "use-cases/agent-memory/"
    assert indexnow.page_url("docs/use-cases/index.md") == SITE + "use-cases/"
    assert indexnow.page_url("docs/index.md") == SITE
    assert indexnow.page_url("server/main.py") is None
    assert indexnow.page_url("docs/benchmarks/results.csv") is None


def test_only_changed_pages_are_submitted_when_the_build_did_not_change() -> None:
    urls = indexnow.urls_for(["docs/a.md", "docs/a.md", "README.md", "docs/b/index.md"])
    assert urls == [SITE + "a/", SITE + "b/"]


def test_the_key_file_is_named_after_its_own_content_and_the_payload_points_at_it() -> None:
    key = indexnow.key()
    assert (ROOT / "docs" / f"{key}.txt").read_text().strip() == key
    body = indexnow.payload([SITE], key)
    assert body["host"] == "clickhouse.github.io"
    assert body["keyLocation"] == f"{SITE}{key}.txt"
    assert body["urlList"] == [SITE]
