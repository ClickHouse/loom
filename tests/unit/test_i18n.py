"""The translation generator keeps code, links, anchors and numbers, and the committed
translations are stamped and link only to files that exist. Freshness is not checked here:
an English edit must not fail every pull request; `.github/workflows/i18n.yml` refreshes
the translations after it lands."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import types
from collections import Counter

import pytest

from scripts.i18n import translate as t
from tests.unit import markdown_links

ROOT = t.ROOT


@pytest.mark.parametrize("source", t.sources())
def test_page_requests_reconstruct_the_anchored_source(source: str) -> None:
    text, _ = t.prepare(source, t.LANGUAGES[0])
    assert "\n".join(t.english_requests(text)) == t.anchor_headings(text, Counter())


def test_a_short_page_goes_in_one_request_and_a_long_one_by_section() -> None:
    short = "# T\n\nintro\n\n## One\n\nx\n\n## Two\n\ny\n"
    assert len(t.english_requests(short)) == 1
    long = short + ("## More\n\n" + "z " * 5000 + "\n") * 2
    assert len(t.english_requests(long)) == 5
    # Anchors stay page-wide across the requests: the second "More" is numbered.
    joined = "\n".join(t.english_requests(long))
    assert '<a id="more"></a>' in joined and '<a id="more-1"></a>' in joined


def test_request_heading_cuts_ignore_fenced_headings() -> None:
    page = "# Title\n\n" + "Loom\n\n" * 1400
    page += "## One\n\n```md\n## not a heading\n```\n\n## Two\n"
    assert [s.split("\n")[2] for s in t.english_requests(page)] == ["# Title", "## One", "## Two"]


def test_anchors_follow_github_slugs_across_sections() -> None:
    seen: Counter[str] = Counter()
    first = t.anchor_headings("## 1. Choose your `Docker` setup\n\n## Notes", seen)
    second = t.anchor_headings("## Notes", seen)
    assert '<a id="1-choose-your-docker-setup"></a>' in first
    assert '<a id="notes"></a>' in first and '<a id="notes-1"></a>' in second


@pytest.mark.parametrize("source", t.sources())
def test_anchors_match_the_ones_the_english_links_are_checked_against(source: str) -> None:
    seen: Counter[str] = Counter()
    page = t.anchor_headings(t.prepare(source, t.LANGUAGES[0])[0], seen)
    ids = re.findall(r'^<a id="([^"]+)"></a>$', page, re.MULTILINE)
    explicit = {
        m.group(1)
        for line, code in t.outside_fences((ROOT / source).read_text())
        if not code and (m := t.ANCHOR.match(line))
    }
    assert set(ids) == markdown_links.anchors(ROOT / source) | explicit


@pytest.fixture
def link_repository(update_repository: None) -> None:
    for source in (
        "docs/use-cases/agent-memory.md",
        "docs/use-cases/multi-agent.md",
        "docs/operations/deployment.md",
        "docs/concepts/memory.md",
    ):
        path = t.ROOT / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Source\n")
    subprocess.run(["git", "add", "docs"], cwd=t.ROOT, check=True)
    for code, source in (
        ("ja", "docs/use-cases/agent-memory.md"),
        ("ko", "docs/use-cases/agent-memory.md"),
        ("ko", "docs/use-cases/multi-agent.md"),
    ):
        path = t.ROOT / t.target_path(code, source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('<a id="measured"></a>\n# Translated\n')


def test_links_point_at_translations_or_the_english_files(link_repository: None) -> None:
    page = (
        "[uc](../use-cases/agent-memory.md#measured) [ops](../operations/deployment.md) "
        "![shot](../images/a.png) [web](https://example.com/x) [here](#local)"
    )
    out = t.rewrite_links(page, "docs/getting-started/quickstart.md", "ja")
    assert "[uc](../use-cases/agent-memory.md#measured)" in out
    assert f"[ops]({t.REPO_URL}/blob/main/docs/operations/deployment.md)" in out
    assert f"![shot]({t.REPO_URL}/raw/main/docs/images/a.png)" in out
    assert "[web](https://example.com/x)" in out and "[here](#local)" in out


def test_html_links_badges_and_reference_definitions_are_rewritten(link_repository: None) -> None:
    page = (
        '[![badge](b.svg)](agent-memory.md) ![i](a.png) <a href="multi-agent.md">m</a> '
        '<a href="../x.md">x</a> <img src="c.png">\n\n[ref]: ../concepts/memory.md\n'
    )
    out = t.rewrite_links(page, "docs/use-cases/index.md", "ko")
    blob, raw = f"{t.REPO_URL}/blob/main/docs", f"{t.REPO_URL}/raw/main/docs"
    assert f"[![badge]({raw}/use-cases/b.svg)](agent-memory.md)" in out
    assert '<a href="multi-agent.md">' in out and f'<a href="{blob}/x.md">' in out
    assert f'<img src="{raw}/use-cases/c.png">' in out
    assert f"[ref]: {blob}/concepts/memory.md" in out


def test_links_inside_code_are_left_alone(link_repository: None) -> None:
    page = 'See `[a](b.md)` and:\n\n```md\n[a](b.md) <a href="c.md">\n```\n\n[a](b.md)\n'
    out = t.rewrite_links(page, "docs/use-cases/index.md", "ja")
    assert out.count("[a](b.md)") == 2 and '<a href="c.md">' in out
    assert f"[a]({t.REPO_URL}/blob/main/docs/use-cases/b.md)" in out


def test_a_translation_must_keep_code_links_and_numbers() -> None:
    source = "Run `loom up` on 3 hosts, see [Loom](https://x.test/a).\n\n```sh\nloom up\n```\n"
    assert t.differences(source, source.replace("Run", "Lancez")) == []
    assert t.differences(source, source.replace("three", "3")) == []
    broken = {
        "inline code": source.replace("`loom up`", "loom up"),
        "link targets": source.replace("https://x.test/a", "https://x.test/b"),
        "numbers": source.replace("3 hosts", "three hosts"),
        "code blocks": source.replace("```sh\nloom up", "```sh\nloom start"),
    }
    for name, text in broken.items():
        assert any(p.startswith(name) for p in t.differences(source, text)), name


def test_a_code_span_may_wrap_differently() -> None:
    source = "Run `loom up\n--fast` here and `x`.\n"
    assert t.differences(source, "Lancez `loom up --fast` ici et `x`.\n") == []


def test_code_blocks_keep_their_order() -> None:
    source = "```sh\nmake migrate\n```\n\n```sh\nmake server\n```\n"
    swapped = "```sh\nmake server\n```\n\n```sh\nmake migrate\n```\n"
    assert t.differences(source, swapped) == [
        "code blocks: copy every fenced code block unchanged, in the same order"
    ]


def test_an_autolink_or_bare_url_must_not_change() -> None:
    source = "Open <http://localhost:7777/docs> or **http://localhost:7777/**.\n"
    for old, new in (
        ("<http://localhost:7777/docs>", "<http://localhost:7777/doc>"),
        ("**http://localhost:7777/**", "**http://127.0.0.1:7777/**"),
    ):
        problems = t.differences(source, source.replace(old, new))
        assert any(p.startswith("URLs") for p in problems), new


def test_a_heading_or_link_text_handed_back_in_english_is_a_difference() -> None:
    source = "## Get a token\n\nSee [agent memory](agent-memory.md) and [Docker](../d.md).\n"
    kept = "## Get a token\n\nVoir [agent memory](agent-memory.md) et [Docker](../d.md).\n"
    assert t.differences(source, kept) == [
        "left in English (headings, link texts, alt texts, table cells): "
        "['Get a token', 'agent memory']"
    ]
    done = "## Obtenir un jeton\n\nVoir [mémoire](agent-memory.md) et [Docker](../d.md).\n"
    assert t.differences(source, done) == []


def test_a_language_name_heading_may_stay_as_it_is() -> None:
    assert t.differences("## Go\n\nText.\n", "## Go\n\nTexto.\n") == []


def test_a_link_to_an_english_only_page_is_translated_too() -> None:
    url = f"{t.REPO_URL}/blob/main/docs/operations/deployment.md"
    source = f"See [Deployment → Providers]({url}).\n"
    assert t.differences(source, f"Voir [Deployment → Providers]({url}).\n")
    assert t.differences(source, f"Voir [Déploiement → Fournisseurs]({url}).\n") == []


def test_alt_texts_html_link_texts_and_table_cells_are_checked() -> None:
    source = (
        '<a href="#x">Get started</a> <img alt="Python SDK on PyPI" src="a.svg">\n'
        "![dashboard showing memories](a.png)\n\n| Benchmark | Score |\n| --- | --- |\n"
        "| LoCoMo (all conversations) | 0.84 |\n| Recall | 1 |\n"
    )
    kept = t.differences(source, source)
    assert kept and all(
        x in kept[0]
        for x in (
            "Get started",
            "Python SDK on PyPI",
            "dashboard showing memories",
            "LoCoMo (all conversations)",
        )
    )
    # A one-word cell ("Recall") may stay: table cells need two words to translate.
    assert "Recall" not in kept[0]


@pytest.mark.parametrize("comparison", ["< 3", "<3", "<= 3"])
def test_html_link_labels_keep_literal_comparisons(comparison: str) -> None:
    source = f'<a href="#limits">Fewer {comparison} values<b>are ready</b></a>'
    assert t.labels(source) == Counter({f"Fewer {comparison} values are ready": 1})


@pytest.mark.parametrize(
    "table",
    ["| values | < 3 |\n| next | 200 |", "| values | < 3      |\n| next | 200     |"],
)
def test_html_tokens_do_not_start_at_markdown_comparisons(table: str) -> None:
    anchor = '<a id="erasure"></a>'
    assert t.HTML_TAG.findall(f"{table}\n\n{anchor}") == ['<a id="erasure">', "</a>"]


@pytest.mark.parametrize(
    "markup",
    [
        '<span data-limit="1 > 0">',
        "<span data-limit='1 > 0'>",
        "</span>",
        "<!-- preserve > inside a comment -->",
        "<!DOCTYPE html>",
        '<?xml version="1.0"?>',
        "<![CDATA[values > limits]]>",
    ],
)
def test_html_tokens_preserve_complete_markup(markup: str) -> None:
    assert t.HTML_TAG.findall(f"Before {markup} after") == [markup]


@pytest.mark.parametrize("name", ["memory_view-panel", "memory.view-panel", "memory-α"])
def test_custom_element_labels_and_attributes_remain_checked(name: str) -> None:
    opening, closing = f'<{name} data-state="ready">', f"</{name}>"
    changed = opening.replace('"ready"', '"waiting"')
    assert t.labels(f'<a href="#x">{opening}Get started{closing}</a>') == Counter(
        {"Get started": 1}
    )
    assert t.HTML_TAG.findall(opening + closing) == [opening, closing]
    assert t.HTML_TAG.findall(changed + closing) != t.HTML_TAG.findall(opening + closing)


def test_a_translation_may_write_a_spelled_number_as_digits() -> None:
    assert t.differences("two steps", "2 Schritte") == []


def test_the_cost_cap_is_checked_before_the_next_call() -> None:
    spend = t.Spend("claude-sonnet-5-5", cap_usd=0.01)
    spend.add({"input_tokens": 1000, "output_tokens": 100})
    spend.before_call()
    spend.add({"input_tokens": 0, "output_tokens": 1000})
    with pytest.raises(SystemExit, match="LOOM_I18N_MAX_USD"):
        spend.before_call()
    assert spend.stopped


def test_a_label_borrowed_from_another_section_keeps_the_page_unwritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    english = "## A\n\nSee [Get a token](t.md).\n\n## B\n\nSee [the docs](t.md).\n" + "x " * 4000
    replies = {
        "A": "## Ä\n\nVoir [Obtenir un jeton](t.md).\n",
        "B": "## Bé\n\nVoir [Get a token](t.md).\n",
    }
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda source, lang: (english, ""))
    monkeypatch.setattr(
        t,
        "translate_text",
        lambda text, lang, client, spend, **_kwargs: replies["A" if "## A" in text else "B"],
    )
    with pytest.raises(t.TranslationDiffers, match="Get a token"):
        t.translate_page("docs/x.md", t.LANGUAGES[1], None, t.Spend("claude-sonnet-5-5", 1.0))
    assert not (tmp_path / "i18n").exists()


@pytest.fixture
def update_repository(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for args in (
        ("init", "-q"),
        ("config", "user.name", "test"),
        ("config", "user.email", "test@example.test"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    for source in ("README.md", "docs/a.md", "docs/b.md"):
        path = tmp_path / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Start\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "English"], cwd=tmp_path, check=True)
    monkeypatch.setattr(t, "ROOT", tmp_path)


def test_a_page_that_still_differs_lets_the_others_finish(
    monkeypatch: pytest.MonkeyPatch,
    update_repository: None,
) -> None:
    written: list[str] = []

    def fake_page(source: str, lang: t.Language, client: object, spend: t.Spend) -> None:
        if source == t.sources()[0]:
            raise t.TranslationDiffers("still differs after a retry")
        written.append(source)
        text, line = t.prepare(source, lang)
        body = "\n".join(t.english_requests(text)).replace("# Start", "# Entrée")
        t.lifecycle.stage(
            t.target_path(lang.code, source), t.render(source, lang, body, line, spend.model)
        )

    monkeypatch.setattr(t, "bedrock_client", lambda: None)
    monkeypatch.setattr(t, "translate_page", fake_page)
    monkeypatch.setattr(t, "write_language_lines", lambda codes=None: None)
    monkeypatch.setattr(t, "request_chars", lambda source, lang: 1)
    with pytest.raises(
        SystemExit, match="pages not written:\ni18n/ja/README.md: still differs after a retry"
    ):
        t.update(every=True, codes=["ja"])
    assert sorted(written) == sorted(t.sources()[1:])


def test_a_fatal_error_after_a_differing_page_still_names_that_page(
    monkeypatch: pytest.MonkeyPatch,
    update_repository: None,
) -> None:
    def fake_page(source: str, lang: t.Language, client: object, spend: t.Spend) -> None:
        if source == t.sources()[0]:
            raise t.TranslationDiffers("still differs after a retry")
        raise SystemExit("Bedrock 500")

    monkeypatch.setattr(t, "bedrock_client", lambda: None)
    monkeypatch.setattr(t, "translate_page", fake_page)
    monkeypatch.setattr(t, "write_language_lines", lambda codes=None: None)
    monkeypatch.setattr(t, "request_chars", lambda source, lang: 1)
    with pytest.raises(SystemExit, match="Bedrock 500"):
        t.update(every=True, codes=["ja"])


def test_code_inside_an_html_link_is_not_a_label() -> None:
    assert t.labels('<a href="#x"><code>loom up</code></a>\n') == {}


def test_a_link_or_alt_text_shown_as_code_is_not_a_label() -> None:
    source = 'Write `<img alt="Diagram of the flow">` or `[the guide](x.md)` in the page.\n'
    assert t.labels(source) == {}
    assert t.labels('<a href="#x"><b>Get started</b></a>\n') == {"Get started": 1}


def test_a_fatal_error_stops_the_other_pages(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    update_repository: None,
) -> None:
    calls: list[str] = []

    def fake_page(source: str, lang: t.Language, client: object, spend: t.Spend) -> None:
        calls.append(source)
        raise SystemExit("boom")

    monkeypatch.setattr(t, "bedrock_client", lambda: None)
    monkeypatch.setattr(t, "translate_page", fake_page)
    monkeypatch.setattr(t, "write_language_lines", lambda codes=None: None)
    monkeypatch.setattr(t, "request_chars", lambda source, lang: 1)
    with pytest.raises(SystemExit, match="boom"):
        t.update(every=True, codes=["ja"])
    assert calls == [t.sources()[0]]
    assert "spent:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args", [["check", "--all"], ["check", "--lang", "ja"], ["update", "--skip-stale"]]
)
def test_each_command_refuses_the_other_ones_flags(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        t.main(args)


def test_another_model_or_banner_makes_every_page_stale(update_repository: None) -> None:
    ja = t.LANGUAGES[1]
    for source in t.sources():
        path = t.ROOT / t.target_path(ja.code, source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(t.header(source, ja, t.DEFAULT_MODEL, t.blob(source)) + "# Inicio\n")
    assert t.stale((ja,), model=t.DEFAULT_MODEL) == []
    assert t.stale((ja,), model="claude-opus-5-5") == [(s, ja) for s in t.sources()]
    edited = t.Language(ja.code, ja.name, ja.english, ja.banner + "!", ja.style)
    assert len(t.stale((edited,))) == len(t.sources())


def test_an_unpriced_model_is_refused(
    monkeypatch: pytest.MonkeyPatch, update_repository: None
) -> None:
    monkeypatch.setenv("LOOM_I18N_MODEL", "some-model")
    with pytest.raises(SystemExit, match="no price"):
        t.update()


def test_the_estimate_defers_pages_outside_the_batch_cap(
    monkeypatch: pytest.MonkeyPatch, update_repository: None
) -> None:
    monkeypatch.setattr(
        t, "bedrock_client", lambda: pytest.fail("deferred pages must not call Bedrock")
    )
    selected, deferred = t.batch(True, ["ja"], 0)
    assert not selected and deferred == len(t.sources())


def test_a_section_that_differs_is_retried_once_with_the_differences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section = "## Run\n\nRun `loom up` twice.\n"
    replies = iter(["## Lancez\n\nLancez loom up.\n", "## Lancez\n\nLancez `loom up` deux fois.\n"])
    sent: list[list[dict[str, str]]] = []

    def fake_call(client: object, model: str, system: str, messages: list[dict[str, str]]) -> dict:
        sent.append(list(messages))
        text = next(replies)
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}], "usage": {}}

    monkeypatch.setattr(t, "call", fake_call)
    out = t.translate_text(section, t.LANGUAGES[4], None, t.Spend("claude-sonnet-5-5", 1.0))
    assert out == "## Lancez\n\nLancez `loom up` deux fois.\n"
    assert len(sent) == 2 and "inline code" in sent[1][-1]["content"]


def test_a_dropped_language_line_is_refused() -> None:
    with pytest.raises(SystemExit, match="dropped the language line"):
        t.render("README.md", t.LANGUAGES[0], "no marker here", "<!-- languages -->x", "m")


def test_a_cut_off_reply_is_refused() -> None:
    with pytest.raises(SystemExit, match="max_tokens"):
        t.reply_text(
            {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]}
        )


def test_the_readme_language_line_is_the_generated_one() -> None:
    assert t.language_lines_differ() == []


def test_a_readme_without_the_markers_stops_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    (tmp_path / "README.md").write_text("# Loom\n\nno language line here\n")
    monkeypatch.setattr(t, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="no <!-- languages --> markers"):
        t.write_language_lines()


def test_a_new_language_is_linked_from_every_readme_without_translating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    for path in t.readmes():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text((ROOT / path).read_text())
    for args in (
        ("init", "-q"),
        ("config", "user.name", "test"),
        ("config", "user.email", "test@example.test"),
        ("add", "."),
        ("commit", "-qm", "Copied documentation"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setattr(t, "ROOT", tmp_path)
    ja = t.LANGUAGES[1]
    extra = t.Language("xx", "Xx", "Xx", ja.banner, ja.style)
    monkeypatch.setattr(t, "LANGUAGES", (*t.LANGUAGES, extra))
    assert t.language_lines_differ() == t.readmes()
    t.write_language_lines()
    assert t.language_lines_differ() == []
    text = (tmp_path / "i18n/ja/README.md").read_text()
    assert "Xx" in text and "../xx/README.md" not in text


def test_translated_readme_links_to_its_existing_site_landing(tmp_path, monkeypatch) -> None:
    from scripts.docs_site import indexnow

    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(indexnow, "SITE", "https://example.com/docs/")
    for source in ("README.md", "docs/index.md", "docs/use-cases/index.md"):
        path = tmp_path / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Source\n")
    monkeypatch.setattr(
        t, "sources", lambda: ("README.md", "docs/index.md", "docs/use-cases/index.md")
    )
    spanish = next(lang for lang in t.LANGUAGES if lang.code == "es")
    for source in ("README.md", "docs/use-cases/index.md"):
        path = tmp_path / t.target_path("es", source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(t.header(source, spanish, t.DEFAULT_MODEL, "a" * 40) + "# Página\n")
    assert '<a href="https://example.com/docs/es/use-cases/">Documentación</a>' in t.language_line(
        "i18n/es/README.md"
    )
    assert "https://example.com" not in t.language_line("README.md")
    path = tmp_path / "i18n/es/docs/index.md"
    path.write_text(t.header("docs/index.md", spanish, t.DEFAULT_MODEL, "a" * 40) + "# Inicio\n")
    assert '<a href="https://example.com/docs/es/">Documentación</a>' in t.language_line(
        "i18n/es/README.md"
    )
    path.unlink()
    (tmp_path / "i18n/es/docs/use-cases/index.md").unlink()
    assert "https://example.com" not in t.language_line("i18n/es/README.md")


def test_generated_readme_uses_the_mkdocs_default_site_base() -> None:
    import yaml

    from scripts.docs_site import indexnow

    config = yaml.compose((ROOT / "mkdocs.yml").read_text())
    site_url = next(value for key, value in config.value if key.value == "site_url")
    assert site_url.tag == "!ENV"
    assert [item.value for item in site_url.value] == ["LOOM_LEGACY_DOCS_URL", indexnow.SITE]


@pytest.mark.parametrize(
    "sources,expected,route",
    [
        (
            ("getting-started/quickstart.md",),
            "getting-started/quickstart.md",
            "getting-started/quickstart/",
        ),
        (("zeta.md", "guide.md"), "guide.md", "guide/"),
        (('guide "advanced".md',), 'guide "advanced".md', "guide%20%22advanced%22/"),
        (("guía & casos.md",), "guía & casos.md", "gu%C3%ADa%20%26%20casos/"),
    ],
)
def test_readme_keeps_a_native_site_link_without_a_translated_index(
    tmp_path, monkeypatch, sources, expected, route
) -> None:
    from html.parser import HTMLParser

    from scripts.docs_site import indexnow, locales

    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "a" and dict(attrs).get("href", "").startswith(indexnow.SITE):
                links.append(dict(attrs))

    monkeypatch.setattr(t, "ROOT", tmp_path)
    language = next(lang for lang in t.LANGUAGES if lang.code == "ja")
    for source in ("README.md", *("docs/" + source for source in sources)):
        english = tmp_path / source
        english.parent.mkdir(parents=True, exist_ok=True)
        english.write_text("# Loom\n")
        target = tmp_path / t.target_path(language.code, source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(t.header(source, language, t.DEFAULT_MODEL, t.blob(source)) + "# Loom\n")
    uri = locales.landing_uri(language.code, locales.translations(tmp_path))
    assert uri == language.code + "/" + expected
    links = []
    Links().feed(t.language_line("i18n/ja/README.md"))
    assert links == [{"href": indexnow.SITE + "ja/" + route}]


def _translations() -> list[tuple[str, t.Language]]:
    return [
        (s, lang)
        for s in t.sources()
        for lang in t.LANGUAGES
        if (ROOT / t.target_path(lang.code, s)).is_file()
    ]


def test_every_use_case_page_is_in_source_inventory() -> None:
    pages = {p.relative_to(ROOT).as_posix() for p in (ROOT / "docs/use-cases").glob("*.md")}
    assert pages <= set(t.sources())


def test_every_output_has_generator_ownership() -> None:
    for path in t.lifecycle.all_files():
        assert t.lifecycle.regular(path)
        if path == t.refresh.PATH:
            assert t.refresh.read() is not None
        else:
            assert t.lifecycle.owner(path, (t.ROOT / path).read_text()) is not None, path


def test_every_translation_exists_with_its_stamp_and_banner() -> None:
    assert _translations()
    for source, lang in _translations():
        text = (ROOT / t.target_path(lang.code, source)).read_text()
        stamp = t.STAMP.match(text)
        assert stamp and stamp.group(1) == source, t.target_path(lang.code, source)
        assert "Machine-translated from the [English page]" in text.split("\n\n", 1)[0]


def test_async_check_still_rejects_a_missing_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ja = t.LANGUAGES[1]
    (tmp_path / "i18n/ja/docs").mkdir(parents=True)
    for name, body in (("a", "[b](b.md#new-heading)\n"), ("b", '<a id="old-heading"></a>\n')):
        source = f"docs/{name}.md"
        (tmp_path / t.target_path("ja", source)).write_text(
            t.header(source, ja, t.DEFAULT_MODEL, "0" * 40) + body
        )
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "sources", lambda: ())
    assert t.check(skip_stale=True) == 1
    assert "which has no such anchor" in capsys.readouterr().out


def test_the_committed_translations_pass_check() -> None:
    """Fresh or not, every committed page keeps its links, anchors and translated labels."""
    assert t.check(skip_stale=True) == 0


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _FakeReply:
    def to_dict(self) -> dict[str, object]:
        return {"stop_reason": "end_turn", "content": [], "usage": {}}


class _FakeMessages:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        self.sent: list[dict[str, object]] = []

    def create(self, **request: object) -> _FakeReply:
        self.sent.append(request)
        if self.errors:
            raise self.errors.pop(0)
        return _FakeReply()


def _fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A stand-in for the `anthropic` package, which the unit environment does not install."""
    module = types.ModuleType("anthropic")
    # As in the SDK, a timeout is a kind of connection error, so `call` must catch it first.
    connection_error = type("APIConnectionError", (Exception,), {})
    module.APIConnectionError = connection_error  # type: ignore[attr-defined]
    module.APITimeoutError = type("APITimeoutError", (connection_error,), {})  # type: ignore[attr-defined]
    module.APIStatusError = _StatusError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setattr(t.time, "sleep", lambda seconds: None)
    return module


@pytest.mark.parametrize("model", list(t.PRICES))
def test_a_call_asks_bedrock_for_the_global_profile_of_the_model(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    _fake_anthropic(monkeypatch)
    messages = _FakeMessages([])
    client = types.SimpleNamespace(messages=messages)
    reply = t.call(client, model, "system", [{"role": "user", "content": "x"}])
    assert reply["stop_reason"] == "end_turn"
    (request,) = messages.sent
    assert request["model"] == f"global.anthropic.{model}"
    assert request["extra_body"] == {"anthropic_version": "bedrock-2023-05-31"}
    assert request["output_config"] == {"effort": "low"} and request["max_tokens"] == t.MAX_TOKENS
    assert request["system"] == "system" and request["timeout"] == 600


def test_a_busy_reply_is_retried_and_a_refusal_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_anthropic(monkeypatch)
    messages = _FakeMessages([_StatusError(503)])
    t.call(types.SimpleNamespace(messages=messages), "claude-sonnet-5-5", "s", [])
    assert len(messages.sent) == 2
    messages = _FakeMessages([_StatusError(403)])
    with pytest.raises(SystemExit, match="Bedrock 403"):
        t.call(types.SimpleNamespace(messages=messages), "claude-sonnet-5-5", "s", [])
    assert len(messages.sent) == 1


def test_a_timeout_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _fake_anthropic(monkeypatch)
    messages = _FakeMessages([module.APITimeoutError("slow")])
    with pytest.raises(SystemExit, match="timed out"):
        t.call(types.SimpleNamespace(messages=messages), "claude-sonnet-5-5", "s", [])
    assert len(messages.sent) == 1


def test_the_client_uses_the_default_credential_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _fake_anthropic(monkeypatch)
    made: list[dict[str, object]] = []
    module.AnthropicBedrock = lambda **options: made.append(options) or "client"  # type: ignore[attr-defined]
    assert t.bedrock_client() == "client"
    # No key, profile or region is passed: the SDK reads the instance role and AWS_REGION.
    assert made == [{"max_retries": 0}]


def test_a_missing_region_or_package_stops_with_what_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _fake_anthropic(monkeypatch)

    def no_region(**options: object) -> None:
        raise ValueError("no region")

    module.AnthropicBedrock = no_region  # type: ignore[attr-defined]
    with pytest.raises(SystemExit, match="set AWS_REGION"):
        t.bedrock_client()
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(SystemExit, match="scripts/i18n/requirements.txt"):
        t.bedrock_client()


def test_a_lost_connection_is_retried_up_to_four_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _fake_anthropic(monkeypatch)
    messages = _FakeMessages([module.APIConnectionError("reset") for _ in range(4)])
    with pytest.raises(SystemExit, match="unreachable"):
        t.call(types.SimpleNamespace(messages=messages), "claude-sonnet-5-5", "s", [])
    assert len(messages.sent) == 4


def test_update_cli_selects_only_rollout_languages_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, list[str] | None]] = []
    monkeypatch.setattr(t, "update", lambda every, codes: calls.append((every, codes)) or 0)
    assert t.ROLLOUT_LANGUAGES == ("zh-CN", "ja", "ko", "pt-BR", "es")
    assert {language.code for language in t.LANGUAGES} == {"zh-CN", "ja", "ko", "pt-BR", "es"}
    assert t.main(["update"]) == 0
    assert calls == [(False, list(t.ROLLOUT_LANGUAGES))]


@pytest.mark.parametrize("code", ["fr", "id", "xx"])
def test_update_cli_refuses_additional_languages_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    ja = t.LANGUAGES[1]
    extra = t.Language(code, code, code, ja.banner, ja.style)
    monkeypatch.setattr(t, "LANGUAGES", (*t.LANGUAGES, extra))
    monkeypatch.setattr(t, "update", lambda *args: pytest.fail("additional locale reached update"))
    with pytest.raises(SystemExit):
        t.main(["update", "--all", "--lang", code])


@pytest.mark.parametrize("code", ["zh-CN", "ja", "ko", "pt-BR", "es"])
def test_update_cli_accepts_each_retained_locale(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    calls: list[tuple[bool, list[str]]] = []
    monkeypatch.setattr(t, "update", lambda every, codes: calls.append((every, codes)) or 0)
    assert t.main(["update", "--lang", code]) == 0
    assert calls == [(False, [code])]


def test_language_line_maintenance_preserves_nonselected_locale_bytes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(t, "ROOT", tmp_path)
    original = "# Loom\n\n<!-- languages -->retained<!-- /languages -->\n"
    paths = ["README.md"] + [t.target_path(lg.code, "README.md") for lg in t.LANGUAGES]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original)
    t.write_language_lines(["ja"])
    for name in paths:
        text = (tmp_path / name).read_text()
        if name in {"README.md", "i18n/ja/README.md"}:
            assert t.language_line(name) in text
        else:
            assert text == original
