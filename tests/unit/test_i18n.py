"""The translation generator keeps code, links, anchors and numbers, and the committed
translations are stamped and link only to files that exist. Freshness is not checked here:
an English edit must not fail every pull request; `.github/workflows/i18n.yml` refreshes
the translations after it lands."""

from __future__ import annotations

import pathlib
import re
import time
from collections import Counter

import pytest

from scripts.i18n import translate as t
from tests.unit import markdown_links

ROOT = t.ROOT


@pytest.mark.parametrize("source", t.SOURCES)
def test_a_page_cuts_into_sections_that_join_back_to_it(source: str) -> None:
    text, _ = t.prepare(source, t.LANGUAGES[0])
    assert "\n".join(t.sections(text)) == text


def test_sections_start_at_level_two_headings_outside_code() -> None:
    page = "# Title\n\nintro\n\n## One\n\n```md\n## not a heading\n```\n\n## Two\n"
    assert [s.split("\n")[0] for s in t.sections(page)] == ["# Title", "## One", "## Two"]


def test_anchors_follow_github_slugs_across_sections() -> None:
    seen: Counter[str] = Counter()
    first = t.anchor_headings("## 1. Choose your `Docker` setup\n\n## Notes", seen)
    second = t.anchor_headings("## Notes", seen)
    assert '<a id="1-choose-your-docker-setup"></a>' in first
    assert '<a id="notes"></a>' in first and '<a id="notes-1"></a>' in second


@pytest.mark.parametrize("source", t.SOURCES)
def test_anchors_match_the_ones_the_english_links_are_checked_against(source: str) -> None:
    seen: Counter[str] = Counter()
    page = t.anchor_headings(t.prepare(source, t.LANGUAGES[0])[0], seen)
    ids = re.findall(r'^<a id="([^"]+)"></a>$', page, re.MULTILINE)
    assert set(ids) == markdown_links.anchors(ROOT / source)


def test_links_point_at_translations_or_the_english_files() -> None:
    page = (
        "[uc](../use-cases/agent-memory.md#measured) [ops](../operations/deployment.md) "
        "![shot](../images/a.png) [web](https://example.com/x) [here](#local)"
    )
    out = t.rewrite_links(page, "docs/getting-started/quickstart.md", "ja")
    assert "[uc](../use-cases/agent-memory.md#measured)" in out
    assert f"[ops]({t.REPO_URL}/blob/main/docs/operations/deployment.md)" in out
    assert f"![shot]({t.REPO_URL}/raw/main/docs/images/a.png)" in out
    assert "[web](https://example.com/x)" in out and "[here](#local)" in out


def test_html_links_badges_and_reference_definitions_are_rewritten() -> None:
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


def test_links_inside_code_are_left_alone() -> None:
    page = 'See `[a](b.md)` and:\n\n```md\n[a](b.md) <a href="c.md">\n```\n\n[a](b.md)\n'
    out = t.rewrite_links(page, "docs/use-cases/index.md", "ja")
    assert out.count("[a](b.md)") == 2 and '<a href="c.md">' in out
    assert f"[a]({t.REPO_URL}/blob/main/docs/use-cases/b.md)" in out


def test_a_translation_must_keep_code_links_and_numbers() -> None:
    source = "Run `loom up` on 3 hosts, see [docs](https://x.test/a).\n\n```sh\nloom up\n```\n"
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
        "untranslated headings or link texts: ['Get a token', 'agent memory']"
    ]
    done = "## Obtenir un jeton\n\nVoir [mémoire](agent-memory.md) et [Docker](../d.md).\n"
    assert t.differences(source, done) == []


def test_a_link_to_an_english_only_page_may_keep_its_title() -> None:
    url = f"{t.REPO_URL}/blob/main/docs/operations/deployment.md"
    source = f"See [Deployment → Providers]({url}).\n"
    assert t.differences(source, f"Voir [Deployment → Providers]({url}).\n") == []


def test_a_translation_may_write_a_spelled_number_as_digits() -> None:
    assert t.differences("two steps", "2 Schritte") == []


def test_the_cost_cap_is_checked_before_the_next_call() -> None:
    spend = t.Spend("claude-sonnet-5-5", cap_usd=0.01)
    spend.add({"input_tokens": 1000, "output_tokens": 100})
    spend.before_call()
    spend.add({"input_tokens": 0, "output_tokens": 1000})
    with pytest.raises(SystemExit, match="LOOM_I18N_MAX_USD"):
        spend.before_call()
    assert spend.stopped.is_set()


def test_the_first_failed_page_stops_the_others(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def fake_page(source: str, lang: t.Language, key: str, spend: t.Spend) -> None:
        if source == t.SOURCES[0]:
            raise SystemExit("boom")
        for _ in range(200):
            spend.before_call()
            calls.append(source)
            time.sleep(0.005)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(t, "translate_page", fake_page)
    monkeypatch.setattr(t, "write_language_lines", lambda: None)
    monkeypatch.setattr(t, "request_chars", lambda source, lang: 1)
    with pytest.raises(SystemExit, match="boom"):
        t.update(every=True, codes=["ja"])
    # Pages already running stop at their next call, and no more than one per worker started.
    assert len(set(calls)) <= t.WORKERS and t.SOURCES[-1] not in calls and len(calls) < 200
    assert "spent:" in capsys.readouterr().out


def test_a_missing_source_stops_check_and_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(t, "SOURCES", (*t.SOURCES, "docs/no-such-page.md"))
    for command in (t.check, t.update):
        with pytest.raises(SystemExit, match="no-such-page.md"):
            command()


@pytest.mark.parametrize(
    "args", [["check", "--all"], ["check", "--lang", "ja"], ["update", "--skip-stale"]]
)
def test_each_command_refuses_the_other_ones_flags(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        t.main(args)


def test_another_model_or_banner_makes_every_page_stale() -> None:
    ja = t.LANGUAGES[1]
    assert t.stale((ja,), model="claude-opus-5-5") == [(s, ja) for s in t.SOURCES]
    edited = t.Language(ja.code, ja.name, ja.english, ja.banner + "!", ja.style)
    assert len(t.stale((edited,))) == len(t.SOURCES)


def test_an_unpriced_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_I18N_MODEL", "some-model")
    with pytest.raises(SystemExit, match="no price"):
        t.update()


def test_the_estimate_refuses_a_run_over_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_I18N_MAX_USD", "0.001")
    monkeypatch.setattr(t, "write_language_lines", lambda: None)
    with pytest.raises(SystemExit, match="nothing translated"):
        t.update(every=True, codes=["ja"])


def test_a_section_that_differs_is_retried_once_with_the_differences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section = "## Run\n\nRun `loom up` twice.\n"
    replies = iter(["## Lancez\n\nLancez loom up.\n", "## Lancez\n\nLancez `loom up` deux fois.\n"])
    sent: list[list[dict[str, str]]] = []

    def fake_call(key: str, model: str, system: str, messages: list[dict[str, str]]) -> dict:
        sent.append(list(messages))
        text = next(replies)
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}], "usage": {}}

    monkeypatch.setattr(t, "call", fake_call)
    out = t.translate_section(section, t.LANGUAGES[4], "key", t.Spend("claude-sonnet-5-5", 1.0))
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
    monkeypatch.setattr(t, "ROOT", tmp_path)
    ja = t.LANGUAGES[1]
    extra = t.Language("xx", "Xx", "Xx", ja.banner, ja.style)
    monkeypatch.setattr(t, "LANGUAGES", (*t.LANGUAGES, extra))
    assert t.language_lines_differ() == t.readmes()
    t.write_language_lines()
    assert t.language_lines_differ() == []
    assert '<a href="../xx/README.md">Xx</a>' in (tmp_path / "i18n/ja/README.md").read_text()


def _translations() -> list[tuple[str, t.Language]]:
    return [
        (s, lang)
        for s in t.SOURCES
        for lang in t.LANGUAGES
        if (ROOT / t.target_path(lang.code, s)).is_file()
    ]


def test_no_translation_is_orphaned() -> None:
    assert t.orphans() == []


def test_every_translation_exists_with_its_stamp_and_banner() -> None:
    assert len(_translations()) == len(t.SOURCES) * len(t.LANGUAGES)
    for source, lang in _translations():
        text = (ROOT / t.target_path(lang.code, source)).read_text()
        stamp = t.STAMP.match(text)
        assert stamp and stamp.group(1) == source, t.target_path(lang.code, source)
        assert "Machine-translated from the [English page]" in text.split("\n\n", 1)[0]


def test_an_anchor_into_a_stale_page_waits_for_that_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    (tmp_path / "i18n/ja").mkdir(parents=True)
    (tmp_path / "i18n/ja/a.md").write_text("[b](b.md#new-heading)\n")
    (tmp_path / "i18n/ja/b.md").write_text('<a id="old-heading"></a>\n')
    monkeypatch.setattr(t, "ROOT", tmp_path)
    assert len(t.link_problems()) == 1
    assert t.link_problems(frozenset({"i18n/ja/b.md"})) == []
    # The other way round: a's link is old, b is new.
    assert t.link_problems(frozenset({"i18n/ja/a.md"})) == []


def test_check_skips_anchors_of_stale_pages_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ja = t.LANGUAGES[1]
    (tmp_path / "i18n/ja").mkdir(parents=True)
    (tmp_path / "i18n/ja/a.md").write_text("[b](b.md#new-heading)\n")
    (tmp_path / "i18n/ja/b.md").write_text('<a id="old-heading"></a>\n')
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "require_sources", lambda: None)
    monkeypatch.setattr(t, "orphans", lambda: [])
    monkeypatch.setattr(t, "untranslated_pages", lambda: [])
    monkeypatch.setattr(t, "language_lines_differ", lambda: [])
    monkeypatch.setattr(t, "stale", lambda model=t.DEFAULT_MODEL: [("b.md", ja)])
    assert t.check(skip_stale=True) == 0
    assert t.check() == 1
    out = capsys.readouterr().out
    assert "i18n/ja/a.md links to b.md#new-heading, which has no such anchor" in out


def test_the_committed_translations_pass_check() -> None:
    """Fresh or not, every committed page keeps its links, anchors and translated labels."""
    assert t.link_problems() == []
    assert t.untranslated_pages() == []
