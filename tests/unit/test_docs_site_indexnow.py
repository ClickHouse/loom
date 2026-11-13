"""The documentation site's IndexNow submission: which URLs a push changed, and the key."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import urlsplit

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
    assert body["keyLocation"] == f"{SITE}{key}.txt"
    assert body["urlList"] == [SITE]


def test_a_change_to_the_build_submits_every_page() -> None:
    every = indexnow.every_page()
    assert SITE in every and SITE + "use-cases/" in every
    for path in (
        "mkdocs.yml",
        "scripts/docs_site/overrides/main.html",
        "scripts/codegen/page_summary.py",
    ):
        assert indexnow.urls_for([path]) == every


def test_every_submitted_url_is_on_the_key_files_host_and_under_its_directory() -> None:
    body = indexnow.payload(indexnow.every_page(), indexnow.key())
    scope = body["keyLocation"].rsplit("/", 1)[0] + "/"
    assert all(u.startswith(scope) for u in body["urlList"])
    assert all(urlsplit(u).hostname == body["host"] for u in body["urlList"])


def test_main_posts_only_the_changed_pages_and_posts_nothing_when_none_changed(monkeypatch) -> None:
    sent: list[dict] = []

    class _Resp:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout):
        sent.append(json.loads(req.data))
        return _Resp()

    monkeypatch.setattr(indexnow.urllib.request, "urlopen", fake_urlopen)
    assert indexnow.main([]) == 0
    assert indexnow.main(["README.md"]) == 0
    assert sent == []
    assert indexnow.main(["docs/a.md"]) == 0
    assert [s["urlList"] for s in sent] == [[SITE + "a/"]]
