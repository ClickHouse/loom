"""Publish the generated translations without changing their repository copies."""

from __future__ import annotations

import hashlib
import html
import posixpath
import re
import sys
from collections.abc import Iterable
from copy import copy
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.codegen.page_summary import page_summary  # noqa: E402
from scripts.docs_site.hooks import _TOKEN, BLOB, _with_source  # noqa: E402
from scripts.docs_site.messages import COPY, SECTION_TITLES  # noqa: E402
from scripts.docs_site.paths import landing_sources, page_path  # noqa: E402
from scripts.docs_site.video_labels import translated_video_labels  # noqa: E402

LANGUAGES = {
    "zh-CN": "简体中文",
    "ja": "日本語",
    "ko": "한국어",
    "pt-BR": "Português (Brasil)",
    "es": "Español",
}
PUBLISHED_LANGUAGES = ("zh-CN", "ja", "ko", "pt-BR", "es")
# Generic language codes do not identify an Open Graph territory.
_OG_LOCALES = {"zh-CN": "zh_CN", "pt-BR": "pt_BR"}
_STAMP = re.compile(r"<!-- i18n: source=(.*?) blob=([0-9a-f]{40}) model=\S+ -->")


def localized_uri(language: str, source: str) -> str:
    """Preserve each source path under its language prefix."""
    return f"{language}/{source}"


def localized_page_path(path: str, *, languages: Iterable[str] | None = None) -> str | None:
    """The site path for an existing or deleted repository translation."""
    parts = path.split("/", 3)
    if (
        len(parts) != 4
        or parts[0] != "i18n"
        or parts[1] not in LANGUAGES
        or parts[1] not in (PUBLISHED_LANGUAGES if languages is None else languages)
        or parts[2] != "docs"
        or not parts[3].endswith(".md")
    ):
        return None
    return quote(page_path(f"docs/{localized_uri(parts[1], parts[3])}"))


def translations(root: Path, *, languages: Iterable[str] | None = None) -> dict[str, dict]:
    pages = {}
    for language in PUBLISHED_LANGUAGES if languages is None else languages:
        for path in sorted((root / "i18n" / language / "docs").rglob("*.md")):
            source = path.relative_to(root / "i18n" / language / "docs").as_posix()
            english = root / "docs" / source
            stamp = _STAMP.match(path.read_text(encoding="utf-8"))
            obsolete = not english.is_file()
            if obsolete and (stamp is None or stamp.group(1) != f"docs/{source}"):
                continue
            blob = None
            if not obsolete:
                content = english.read_bytes()
                blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            pages[localized_uri(language, source)] = {
                "language": language,
                "source": source,
                "path": str(path),
                "obsolete": obsolete,
                "stale": obsolete
                or stamp is None
                or stamp.group(1) != f"docs/{source}"
                or stamp.group(2) != blob,
            }
    return pages


def on_files(files, config):
    from mkdocs.structure.files import File, InclusionLevel

    pages = config.extra["locale_pages"] = translations(ROOT)
    for uri, entry in pages.items():
        entry["title"] = page_summary(Path(entry["path"]))[0]
        files.append(
            File.generated(
                config,
                uri,
                abs_src_path=entry["path"],
                inclusion=InclusionLevel.NOT_IN_NAV,
            )
        )
    return files


def alternates(source: str, pages: dict, site_url: str) -> list[dict[str, str]]:
    available = [
        {
            "lang": entry["language"],
            "name": LANGUAGES[entry["language"]],
            "url": site_url + quote(page_path(f"docs/{uri}")),
        }
        for uri, entry in pages.items()
        if entry["source"] == source and not entry["obsolete"]
    ]
    if not available:
        return []
    english = {
        "lang": "en",
        "name": "English",
        "url": site_url + quote(page_path(f"docs/{source}")),
    }
    return [english, *available, {**english, "lang": "x-default"}]


def language_landings(pages: dict, site_url: str) -> list[dict[str, str]]:
    landings = []
    for language, name in LANGUAGES.items():
        uri = landing_uri(language, pages)
        if uri:
            landings.append(
                {"lang": language, "name": name, "url": site_url + quote(page_path(f"docs/{uri}"))}
            )
    return landings


def landing_uri(language: str, pages: dict) -> str | None:
    available = landing_sources(
        uri.removeprefix(language + "/")
        for uri, entry in pages.items()
        if entry["language"] == language and not entry["obsolete"]
    )
    return localized_uri(language, available[0]) if available else None


def language_choices(source: str, pages: dict, site_url: str) -> list[dict]:
    """Offer an equivalent page or a named existing landing, never a missing URL."""
    choices = [
        {
            "lang": "en",
            "name": "English",
            "url": site_url + quote(page_path(f"docs/{source}")),
            "landing": "",
        }
    ]
    for landing in language_landings(pages, site_url):
        code, name = landing["lang"], landing["name"]
        equivalent = localized_uri(code, source)
        uri = (
            equivalent
            if equivalent in pages and not pages[equivalent]["obsolete"]
            else landing_uri(code, pages)
        )
        if uri is None:
            continue
        entry = pages[uri]
        choices.append(
            {
                "lang": code,
                "name": name,
                "url": site_url + quote(page_path(f"docs/{uri}")),
                "landing": ""
                if uri == equivalent
                else entry.get("title") or page_summary(Path(entry["path"]))[0],
            }
        )
    return choices


def on_nav(nav, config, files):
    config.extra["locale_nav_pages"] = {
        file.src_uri: file.page for file in files if file.src_uri in config.extra["locale_pages"]
    }
    return nav


def localized_navigation(nav, page, config):
    """Project the shared English navigation without changing any shared node."""
    language = page.meta["language"]
    pages = config.extra["locale_pages"]
    localized = config.extra["locale_nav_pages"]
    projected = []
    has_fallback = False

    def item_view(item, parent=None):
        nonlocal has_fallback
        target = item
        entry = None
        if item.is_page:
            uri = localized_uri(language, item.file.src_uri)
            entry = pages.get(uri)
            if entry and not entry["obsolete"]:
                target = localized[uri]
            else:
                entry = None
                has_fallback = True
        view = copy(target)
        view.parent = parent
        if item.is_section:
            view.children = [item_view(child, view) for child in item.children]
            view.title = SECTION_TITLES[language].get(
                item.title, f"{item.title} ({COPY[language]['english']})"
            )
            view.active = any(child.active for child in view.children)
        elif item.is_page:
            view.typeset = None
            view.title = (
                (entry.get("title") or page_summary(Path(entry["path"]))[0])
                if entry
                else f"{item.title} ({COPY[language]['english']})"
            )
            view.active = view.file.src_uri == page.file.src_uri
            projected.append(view)
        return view

    result = copy(nav)
    result.items = [item_view(item) for item in nav.items]
    result.pages = projected
    for index, item in enumerate(projected):
        item.previous_page = projected[index - 1] if index else None
        item.next_page = projected[index + 1] if index + 1 < len(projected) else None
    home = landing_uri(language, pages)
    if home:
        result.homepage = localized[home]
    return result, has_fallback


def on_page_context(context, page, config, nav):
    if page.meta.get("language", "en") == "en":
        return context
    projected, fallback = localized_navigation(nav, page, config)
    context["nav"] = projected
    context["locale_has_english_fallback"] = fallback
    current = next(
        (item for item in projected.pages if item.file.src_uri == page.file.src_uri), None
    )
    if current:
        context["page"] = current
    return context


def _link_target(target: str, entry: dict, pages: dict, site_url: str, *, banner: bool) -> str:
    language = entry["language"]
    if target.startswith(BLOB + "docs/"):
        if not banner:
            # The producer chooses English when a translated anchor is absent.
            return target
        source = unquote(target[len(BLOB + "docs/") :])
    elif target.startswith(BLOB + "i18n/"):
        parts = unquote(target[len(BLOB + "i18n/") :]).split("/", 2)
        if len(parts) != 3 or parts[1] != "docs":
            return target
        language, _, source = parts
    elif target and not re.match(r"(?:[a-zA-Z][\w+.-]*:|/)", target):
        source = posixpath.normpath(
            posixpath.join(posixpath.dirname(entry["source"]), unquote(target))
        )
        if source.startswith("../"):
            repository = posixpath.normpath(posixpath.join("docs", source))
            translated = f"i18n/{language}/{repository}"
            return BLOB + quote(translated if (ROOT / translated).is_file() else repository)
    else:
        return target
    uri = localized_uri(language, source)
    if not banner and uri in pages:
        return site_url + quote(page_path(f"docs/{uri}"))
    if not (ROOT / "docs" / source).is_file():
        return target
    if not source.endswith(".md"):
        return site_url + quote(source)
    if banner or uri not in pages:
        uri = source
    return site_url + quote(page_path(f"docs/{uri}"))


def translated_markdown(markdown: str, entry: dict, pages: dict, site_url: str) -> str:
    # Explicit English fallbacks retain the producer's anchor-safety decision.
    header, separator, body = markdown.partition("\n\n")
    if entry.get("obsolete"):
        if not separator:
            header, _, body = markdown.partition("\n")
            separator = "\n\n"
        header = header.split("\n", 1)[0]
        ui = COPY[entry["language"]]
        body = (
            f'<p role="note" lang="{entry["language"]}">{ui["retained_before"]}'
            f'<a href="{html.escape(site_url, quote=True)}">{ui["english_documentation"]}</a>'
            f"{ui['retained_after']}</p>\n\n{body}"
        )

    def rewrite(text: str, *, banner: bool) -> str:
        def replace(match: re.Match[str]) -> str:
            if not match.group("link") or not match.group("target"):
                return match.group(0)
            target = _link_target(match.group("target"), entry, pages, site_url, banner=banner)
            return (
                f"{match.group('bang')}{match.group('text')}{target}"
                f"{match.group('anchor') or ''}{match.group('tail')}"
            )

        return _TOKEN.sub(replace, text)

    return rewrite(header, banner=True) + separator + rewrite(body, banner=False)


def on_page_markdown(markdown, page, config, files):
    pages = config.extra["locale_pages"]
    uri = page.file.src_uri
    entry = pages.get(uri)
    source = entry["source"] if entry else uri
    page.meta["language"] = entry["language"] if entry else "en"
    page.meta["locale_ui"] = COPY[page.meta["language"]]
    page.meta["theme_language"] = {"zh-CN": "zh"}.get(page.meta["language"], page.meta["language"])
    page.meta["og_locale"] = _OG_LOCALES.get(page.meta["language"])
    page.meta["alternates"] = alternates(source, pages, config.site_url)
    choice_source = "index.md" if entry and entry["obsolete"] else source
    choices = page.meta["language_choices"] = language_choices(
        choice_source, pages, config.site_url
    )
    current_url = config.site_url + quote(page_path(f"docs/{uri}"))
    for choice in choices:
        choice["current"] = choice["url"] == current_url
    if not entry:
        return markdown
    page.edit_url = None
    page.meta["title"] = page.title = page_summary(Path(entry["path"]))[0]
    page.meta["translation_stale"] = entry["stale"]
    page.meta["translation_obsolete"] = entry["obsolete"]
    return translated_markdown(markdown, entry, pages, config.site_url)


def on_post_page(output, page, config):
    language = html.escape(page.meta.get("language", "en"), quote=True)
    if language != "en":
        output = translated_video_labels(output)
    return re.sub(r'(<html\b[^>]*\blang=")[^"]*(")', rf"\g<1>{language}\2", output, count=1)


def on_post_build(config):
    site = Path(config.site_dir)
    pages = config.extra["locale_pages"]
    for uri, entry in pages.items():
        body = translated_markdown(
            Path(entry["path"]).read_text(encoding="utf-8"), entry, pages, config.site_url
        )
        if entry["stale"] and not entry["obsolete"]:
            source_url = config.site_url + quote(page_path(f"docs/{entry['source']}"))
            ui = COPY[entry["language"]]
            body = (
                f"> {ui['stale_before']}[{ui['english_page']}]({source_url})"
                f"{ui['stale_after']}\n\n{body}"
            )
        text = _with_source(body, config.site_url, quote(uri))
        places = [site / uri, site / page_path(f"docs/{uri}") / "index.html.md"]
        if uri.endswith("/index.md"):
            places.append(site / f"{uri[: -len('/index.md')]}.md")
        for out in places:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
