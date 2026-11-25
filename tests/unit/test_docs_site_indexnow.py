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

ORIGIN = "https://loom.clickhouse.com"
SITE = f"{ORIGIN}/docs/"


def test_a_changed_page_maps_to_the_url_the_site_serves_it_at() -> None:
    assert indexnow.page_url("docs/use-cases/agent-memory.md") == SITE + "use-cases/agent-memory/"
    assert indexnow.page_url("docs/use-cases/index.md") == SITE + "use-cases/"
    assert indexnow.page_url("docs/index.md") == SITE
    assert indexnow.page_url("server/main.py") is None
    assert indexnow.page_url("docs/benchmarks/results.csv") is None


def test_only_changed_pages_are_submitted_without_translation_dependencies(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(indexnow, "DOCS", tmp_path / "docs")
    urls = indexnow.urls_for({"docs/a.md": "M", "README.md": "M", "docs/b/index.md": "M"})
    assert urls == [SITE + "a/", SITE + "b/"]


def test_astro_pages_submit_their_canonical_public_urls() -> None:
    urls = indexnow.urls_for(
        {
            "docs/pages/home.astro": "M",
            "docs/pages/ja/home.astro": "A",
            "docs/pages/platform.astro": "M",
            "docs/pages/adoption.astro": "M",
        }
    )
    assert urls == [f"{ORIGIN}/", f"{ORIGIN}/adoption", f"{ORIGIN}/ja", f"{ORIGIN}/platform"]
    assert indexnow.payload(urls, "a" * 32)["urlList"] == urls


def test_public_components_and_shared_build_inputs_submit_affected_pages() -> None:
    public_pages = sorted(
        f"{ORIGIN}{route}"
        for route in (
            "/",
            "/adoption",
            "/ar",
            "/de",
            "/de/enterprise",
            "/enterprise",
            "/es",
            "/es/enterprise",
            "/fr",
            "/fr/enterprise",
            "/hi",
            "/id",
            "/it",
            "/ja",
            "/ko",
            "/platform",
            "/pt-BR",
            "/pt-BR/enterprise",
            "/ru",
            "/telemetry",
            "/tr",
            "/vi",
            "/zh-CN",
            "/zh-Hant",
        )
    )
    assert indexnow.urls_for({"docs/components/SiteFrame.astro": "M"}) == public_pages
    assert indexnow.urls_for({"docs/lib/adoption-data.mjs": "M"}) == public_pages
    assert indexnow.urls_for({"docs/lib/adoption-data.test.mjs": "M"}) == []
    assert indexnow.urls_for({"docs/scripts/public-pages.json": "M"}) == public_pages
    assert indexnow.urls_for({"docs/scripts/home-locales.json": "M"}) == public_pages
    all_pages = sorted([*public_pages, *indexnow.every_page()])
    assert indexnow.urls_for({"docs/theme.css": "M"}) == all_pages
    assert indexnow.urls_for({"docs/lib/site-identity.mjs": "M"}) == all_pages


def test_changed_translation_availability_submits_homepage_links() -> None:
    translation = "i18n/ja/docs/use-cases/knowledge-factory.md"
    for status in ("A", "D"):
        urls = indexnow.urls_for({translation: status})
        assert SITE + "ja/use-cases/knowledge-factory/" in urls
        assert ORIGIN + "/ja" in urls
        assert ORIGIN + "/platform" not in urls


def test_renamed_and_deleted_public_routes_keep_their_previous_urls(monkeypatch) -> None:
    monkeypatch.setattr(
        indexnow, "PUBLIC_PAGE_URLS", {"docs/pages/product.astro": ORIGIN + "/product"}
    )
    assert indexnow.urls_for(
        {
            "docs/pages/platform.astro": "D",
            "docs/pages/product.astro": "A",
            indexnow.PUBLIC_PAGES_PATH: "M",
        },
        previous_public_routes={"/platform": "/docs/platform", "/retired": "/docs/retired"},
    ) == [ORIGIN + "/platform", ORIGIN + "/product", ORIGIN + "/retired"]


def test_list_mode_prints_changed_urls_without_reading_the_key(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(indexnow, "DOCS", tmp_path / "docs")
    assert indexnow.main({"README.md": "M"}, list_urls=True) == 0
    assert capsys.readouterr().out == ""
    assert indexnow.main({"docs/pages/home.astro": "M"}, list_urls=True) == 0
    assert capsys.readouterr().out == f"{ORIGIN}/\n"


def test_git_changes_preserve_both_routes_of_a_rename(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(indexnow, "DOCS", tmp_path / "docs")
    records = tmp_path / "changes"
    records.write_bytes(b"D\0docs/old name.md\0A\0docs/new name.md\0")
    changes = indexnow.read_changes(records)
    assert changes == {"docs/old name.md": "D", "docs/new name.md": "A"}
    assert indexnow.urls_for(changes) == [SITE + "new%20name/", SITE + "old%20name/"]


def test_full_submission_only_enumerates_documentation_sources(tmp_path, monkeypatch) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(indexnow, "DOCS", docs)
    for name in ("a.md", ".blume-content/a.md", ".vercel/a.md", "dist/a.md", "node_modules/a.md"):
        path = docs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# A\n")
    assert indexnow.every_page() == [SITE + "a/"]


def test_the_key_file_is_named_after_its_own_content_and_the_payload_points_at_it() -> None:
    key = indexnow.key()
    assert (ROOT / "docs" / f"{key}.txt").read_text().strip() == key
    body = indexnow.payload([SITE], key)
    assert body["keyLocation"] == f"https://loom.clickhouse.com/{key}.txt"
    assert body["urlList"] == [SITE.rstrip("/")]


def test_a_change_to_the_build_submits_every_page() -> None:
    every = indexnow.every_page()
    assert SITE in every and SITE + "use-cases/" in every
    for path in (
        "mkdocs.yml",
        "scripts/docs_site/overrides/main.html",
        "scripts/docs_site/locales.py",
        "scripts/docs_site/video_labels.py",
        "scripts/docs_site/paths.py",
        "scripts/docs_site/hooks.py",
        "scripts/docs_site/requirements.txt",
        "scripts/codegen/page_summary.py",
    ):
        assert indexnow.urls_for({path: "M"}) == every
    # Changing the submitter itself, or a file that only looks like a build input, renders
    # nothing differently.
    assert indexnow.urls_for({"scripts/docs_site/indexnow.py": "M"}) == []
    assert indexnow.urls_for({"mkdocs.yml.orig": "M"}) == []
    assert indexnow.urls_for({"scripts/docs_site/overrides-old/x.html": "M"}) == []
    # A page deleted in the same push is not in the tree; its URL is still announced.
    assert SITE + "gone-page/" in indexnow.urls_for({"mkdocs.yml": "M", "docs/gone-page.md": "D"})


def test_every_submitted_url_is_on_the_key_files_host_and_under_its_directory() -> None:
    body = indexnow.payload(indexnow.every_page(), indexnow.key())
    scope = body["keyLocation"].rsplit("/", 1)[0] + "/"
    assert all(u.startswith(scope) for u in body["urlList"])
    assert all(urlsplit(u).hostname == body["host"] for u in body["urlList"])


def test_main_posts_only_the_changed_pages_and_posts_nothing_when_none_changed(
    tmp_path, monkeypatch
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    key = "a" * 32
    (docs / f"{key}.txt").write_text(key)
    monkeypatch.setattr(indexnow, "DOCS", docs)
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
    assert indexnow.main({}) == 0
    assert indexnow.main({"README.md": "M"}) == 0
    assert sent == []
    assert indexnow.main({"docs/a.md": "M"}) == 0
    assert [s["urlList"] for s in sent] == [[SITE + "a"]]
    previous = tmp_path / "previous-public-pages.json"
    previous.write_text(json.dumps({"/retired": "/docs/retired"}))
    assert indexnow.main({indexnow.PUBLIC_PAGES_PATH: "M"}, previous) == 0
    assert ORIGIN + "/retired" in sent[-1]["urlList"]


def test_shared_language_route_mapping_is_a_render_input() -> None:
    from scripts.docs_site import locales

    helper = Path(locales.page_path.__code__.co_filename).resolve().relative_to(ROOT)
    assert indexnow.urls_for({helper.as_posix(): "M"}) == indexnow.every_page()
